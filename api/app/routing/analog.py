"""An analog line through a gateway (research N8): the business line you already pay for as a flat-rate route.

Most US business lines include local calls at no extra charge. Through an FXO
gateway with T.38 on the local network (``sip_trunk.ANALOG_GATEWAYS``: Grandstream
HT813 and GXW410x, Patton SmartNode SN4112/SN4114, AudioCodes MediaPack MP-114/118),
such a line is a Faxbot trunk account whose local calls cost $0 at the margin.
Nothing new chooses it: this module records the line's prices so the planner's
usual price ranking (``origin_rates``, ``predict_facts``) picks it.

- **The line's rate card.** One sending card for the trunk account: the line's
  monthly fee, and the per-minute price for calls outside the local area that
  you enter from your phone bill or your carrier's tariff (0 when your plan
  includes them). Without that price no card is saved, so a toll call on the
  line stays unpriced, never $0.
- **The local calling area.** A list of North American prefixes (NPA-NXX) your
  line reaches as local calls, imported from a file you saved: the Local
  Calling Guide's "Local prefixes" page for your line's prefix (saved as HTML:
  the site offers no download and says "you may, however, save search results as
  HTML, and process them yourself", localcallingguide.com/saq.php, read
  2026-10-10), its XML answer, a list from your carrier, or one prefix a line.
  Each becomes a $0 price row for ``1NPANXX`` on the line's card; rows you saved
  for other prefixes on that card are kept. A new import replaces the earlier
  local rows (which are kept as history, never changed). Faxbot never queries
  the Local Calling Guide itself: its operator asks automated users to cache
  results and publishes no terms, so ask the operator before automating it.
- **Dialing.** ``local_area`` dials a local number with ten digits and any other
  with 1 and ten digits, for exchanges that refuse 1 before a local number
  (``sip_trunk.dial_number``, ``is_local``); ``local`` dials 1 and ten digits for
  every call. ``*70`` before the number cancels call waiting on that call where
  the exchange offers it.
- **One call per line.** The gateway presets carry one call at a time unless you
  set Calls at once to the number of lines you connect (``capacity``).

A V.34 (33.6 kbit/s) fax modem is reachable only on a line like this one: a
hardware Class 1.0 modem on the analog line under HylaFAX+ (``Class1EnableV34Cmd``),
instead of a gateway. Faxbot's engines run V.17 at most; that hookup is a note,
not something Faxbot sets up.

Not yet run against a real gateway, line or carrier bill.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
import io
import re
import xml.etree.ElementTree as ElementTree


MAX_BYTES = 4_000_000
MAX_PREFIXES = 20_000
NPA_NXX = re.compile(r'[2-9][0-9]{2}')
LCG_SAVE = 'https://www.localcallingguide.com/saq.php'


class AnalogLineError(ValueError):
    """One plain sentence for the administrator."""


@dataclass
class LocalList:
    """Prefixes read from one file: ``(npa, nxx)`` pairs, the calling plans the file names (with how many prefixes
    each), and what kind of file it was."""
    prefixes: list = field(default_factory=list)
    plans: dict = field(default_factory=dict)
    kind: str = 'text'


# Reading the file -----------------------------------------------------------------------------------------------

class _Tables(HTMLParser):
    """Every table row's cell texts, in order."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self._row, self._cell = [], None, None

    def handle_starttag(self, tag, attrs):
        if tag == 'tr':
            self._row = []
        elif tag in ('td', 'th') and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ('td', 'th') and self._row is not None and self._cell is not None:
            self._row.append(' '.join(''.join(self._cell).split()))
            self._cell = None
        elif tag == 'tr' and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def _column(header, *names):
    lowered = [cell.strip().lower() for cell in header]
    for name in names:
        if name in lowered:
            return lowered.index(name)
    return None


def _from_rows(rows, found):
    """Rows with a header naming NPA and NXX (and maybe Plan Type): the prefixes under it."""
    header = None
    for row in rows:
        if header is None:
            if _column(row, 'npa') is not None and _column(row, 'nxx') is not None:
                header = (_column(row, 'npa'), _column(row, 'nxx'), _column(row, 'plan type', 'plan'))
            continue
        npa_at, nxx_at, plan_at = header
        if len(row) <= max(npa_at, nxx_at):
            continue
        npa, nxx = row[npa_at].strip(), row[nxx_at].strip()
        if NPA_NXX.fullmatch(npa) and NPA_NXX.fullmatch(nxx):
            plan = row[plan_at].strip() if plan_at is not None and len(row) > plan_at else ''
            found.append((npa, nxx, plan))
    return header is not None


def _local_name(tag):
    return tag.rsplit('}', 1)[-1].lower()


def read_local_list(data, filename='') -> LocalList:
    """The local prefixes in a saved Local Calling Guide page (HTML or XML), a CSV with NPA and NXX columns, or a
    text list with one prefix a line (303-426, 303 426, 1303426). Raises AnalogLineError."""
    if isinstance(data, bytes):
        if len(data) > MAX_BYTES:
            raise AnalogLineError('This file is larger than 4 MB. Save only the list of local prefixes for your line.')
        data = data.decode('utf-8', errors='replace')
    if len(data) > MAX_BYTES:
        raise AnalogLineError('This file is larger than 4 MB. Save only the list of local prefixes for your line.')
    text = data.lstrip('﻿')
    head = text[:2000].lower()
    found, kind = [], 'text'
    if '<!doctype' in head and '<html' not in head or '<!entity' in text.lower():
        raise AnalogLineError('Faxbot does not read XML files with document type declarations. Save the list as an '
                              'HTML page or one prefix a line.')
    if head.lstrip().startswith('<?xml') or (head.lstrip().startswith('<') and '<html' not in head
                                              and '<table' not in head):
        kind = 'xml'
        try:
            root = ElementTree.fromstring(text)
        except ElementTree.ParseError:
            raise AnalogLineError('This XML file could not be read. Save it again from your browser.') from None
        for element in root.iter():
            children = {_local_name(child.tag): (child.text or '').strip() for child in element}
            npa, nxx = children.get('npa', ''), children.get('nxx', '')
            if NPA_NXX.fullmatch(npa) and NPA_NXX.fullmatch(nxx):
                found.append((npa, nxx, ''))
    elif '<table' in head or '<html' in head or '<tr' in text[:200000].lower():
        kind = 'html'
        parser = _Tables()
        parser.feed(text)
        parser.close()
        if not _from_rows(parser.rows, found):
            raise AnalogLineError('This page has no table with NPA and NXX columns. Save the Local prefixes page for '
                                  "your line's prefix from the Local Calling Guide.")
    else:
        rows = list(csv.reader(io.StringIO(text)))
        if not _from_rows(rows, found):
            for line in text.splitlines():
                match = re.match(r'\s*"?(?:\+?1[\s.-]?)?\(?([2-9][0-9]{2})\)?[\s,;/.-]*([2-9][0-9]{2})(?![0-9])', line)
                if match:
                    found.append((match[1], match[2], ''))
        kind = 'csv' if rows and any(len(row) > 1 for row in rows[:5]) else 'text'
    result = LocalList(kind=kind)
    seen = set()
    for npa, nxx, plan in found:
        if plan:
            result.plans[plan] = result.plans.get(plan, 0) + 1
        result.prefixes.append((npa, nxx, plan))
        seen.add((npa, nxx))
    if len(seen) > MAX_PREFIXES:
        raise AnalogLineError(f'This list has more than {MAX_PREFIXES:,} prefixes; a local calling area has fewer. '
                              "Save the list for your line's own prefix.")
    return result


def choose_plan(found: LocalList, plan=None):
    """The ``(npa, nxx)`` prefixes of one calling plan: the only one, or the one you name. Raises AnalogLineError."""
    plans = found.plans
    if plan:
        wanted = plan.strip().lower()
        names = [name for name in plans if name.lower() == wanted]
        if not names:
            listed = ', '.join(sorted(plans)) or 'none'
            raise AnalogLineError(f'The file has no calling plan called {plan.strip()}. Its plans: {listed}.')
        chosen = {(npa, nxx) for npa, nxx, item in found.prefixes if item == names[0]}
    else:
        if len(plans) > 1:
            listed = '; '.join(f'{name} ({count} prefixes)' for name, count in sorted(plans.items()))
            raise AnalogLineError(f'This list has several calling plans: {listed}. Choose the plan your line has.')
        chosen = {(npa, nxx) for npa, nxx, _ in found.prefixes}
    if not chosen:
        raise AnalogLineError('No local prefixes were found in this file. Save the Local prefixes page for your '
                              "line's prefix, or a list with one prefix a line, such as 303-426.")
    return sorted(chosen)


def line_prefix(text):
    """``(npa, nxx)`` from 303-426, (303) 426, 1-303-426 or a full number such as +1 303 426 0100."""
    digits = re.sub(r'[^0-9]', '', str(text or ''))
    if len(digits) in (7, 11) and digits.startswith('1'):
        digits = digits[1:]
    if len(digits) >= 6 and NPA_NXX.fullmatch(digits[:3]) and NPA_NXX.fullmatch(digits[3:6]):
        return digits[:3], digits[3:6]
    raise AnalogLineError("Enter your line's area code and exchange, such as 303-426, or its full number.")


# The line and its card -----------------------------------------------------------------------------------------------

def line_account(values, account):
    """The trunk account ``account`` when it is an analog line, else raises AnalogLineError."""
    from .. import sip_trunk
    found = sip_trunk.trunk_for(values, account or sip_trunk.PRIMARY)
    preset = sip_trunk.PRESETS.get(getattr(found.values, 'sip_trunk_preset', '') or '') if found else None
    if found is None or preset is None or not preset.analog_line:
        raise AnalogLineError('This trunk is not an analog line. Choose an analog line gateway as its carrier first.')
    return found, preset


def card_identity(account, preset_id):
    """The card that prices the line's calls: the account's own key, or sip-<preset> for the first trunk."""
    from .. import sip_trunk
    return f'sip-{preset_id}' if (account or sip_trunk.PRIMARY) == sip_trunk.PRIMARY else account


def _zero(row):
    return row.per_minute_micros == 0 and row.per_page_micros == 0 and row.per_call_micros == 0


def _local_row(row):
    return _zero(row) and len(row.destination_prefix) == 7 and row.destination_prefix.startswith('1')


def local_rows(engine, identity):
    """The line's current $0 local rows (``origin_rates.OriginRate``)."""
    from .origin_rates import saved
    return [row for row in saved(engine, [identity]) if _local_row(row)]


def is_local(engine, account, preset_id, number) -> bool:
    """Whether ``number`` is in the local calling area imported for analog line ``account`` (gateway preset
    ``preset_id``). An unreadable list (``origin_rates.saved`` logs nothing and reads as none) dials the number as a
    phone here dials it, 1 and ten digits."""
    if engine is None or not number:
        return False
    digits = re.sub(r'[^0-9]', '', str(number))
    return any(digits.startswith(row.destination_prefix)
               for row in local_rows(engine, card_identity(account, preset_id)))


def import_local_calls(engine, store, values, account, data, *, filename='', plan=None, line=None,
                       toll_per_minute=None, monthly_fee=None, increment=60, source_url=None, now=None):
    """Save the line's card (when a toll price is given) and its local calling area as $0 rows; returns the view."""
    from .costs import InvalidRateCard, RateCard, parse_amount
    from .origin_rates import OriginRate, save_rows, saved
    found_account, preset = line_account(values, account)
    found = read_local_list(data, filename)
    prefixes = choose_plan(found, plan)
    if line:
        npa, nxx = line_prefix(line)
        if (npa, nxx) not in set(prefixes):
            raise AnalogLineError(f"Your line's own prefix {npa}-{nxx} is not in this list. Save the list for your "
                                  "line's prefix.")
    identity = card_identity(account, preset.id)
    now = now or datetime.utcnow().replace(microsecond=0)
    current = next((card for card in store.current_cards() if card.provider_id == identity
                    and card.direction == 'outbound'), None)
    if toll_per_minute is None and current is None:
        raise AnalogLineError('Enter what your line charges a minute for calls outside the local area, from your '
                              'phone bill or your carrier; enter 0 if your plan includes them.')
    if source_url is not None and (len(source_url) > 512 or not re.match(r'https?://', source_url)):
        raise AnalogLineError('Enter the address of the page the list came from, starting with https://.')
    if toll_per_minute is not None or monthly_fee is not None:
        try:
            card = RateCard(None, identity, 'outbound', (found_account.label or preset.label)[:100],
                            current.currency if current else 'USD',
                            parse_amount(str(toll_per_minute)) if toll_per_minute is not None
                            else current.per_minute_micros, 0, 0, int(increment),
                            int(increment), None, now,
                            parse_amount(str(monthly_fee)) if monthly_fee not in (None, '')
                            else (current.monthly_fee_micros if current else None))
        except (InvalidRateCard, ValueError, TypeError) as error:
            raise AnalogLineError(str(error) if isinstance(error, InvalidRateCard)
                                  else 'Enter prices as amounts such as 0.05.') from None
        others = [item for item in store.current_cards()
                  if not (item.provider_id == identity and item.direction == 'outbound')]
        store.replace_cards(others + [card])
        current = next(item for item in store.current_cards() if item.provider_id == identity
                       and item.direction == 'outbound')
    kept = [row for row in saved(engine, [identity]) if not _local_row(row)]
    rows = kept + [OriginRate(identity, 'any', f'1{npa}{nxx}', current.currency, 0, 0, 0, current.billing_increment_seconds,
                              0, source_url, now) for npa, nxx in prefixes]
    save_rows(engine, current.id, rows, now=now)
    return line_view(engine, store, values, account)


def line_view(engine, store, values, account):
    """What Providers → the analog line shows: its local area, its prices and one sentence."""
    from .costs import rate_text
    try:
        found_account, preset = line_account(values, account)
    except AnalogLineError:
        return {'analog': False, 'account': account or 'sip'}
    from ..capacity import trunk_calls_at_once
    identity = card_identity(account, preset.id)
    card = next((item for item in store.current_cards() if item.provider_id == identity
                 and item.direction == 'outbound'), None)
    rows = local_rows(engine, identity)
    imported = max((row.captured_on for row in rows if row.captured_on), default=None)
    sources = sorted({row.source_url for row in rows if row.source_url})
    toll = rate_text(card) if card is not None else None
    fee = None
    if card is not None and card.monthly_fee_micros:
        from .delivered import short_money_text
        fee = short_money_text(card.monthly_fee_micros, card.currency)
    if not rows:
        sentence = ('No local calling area yet: import the list of local prefixes for this line, so local numbers '
                    'go out on it at no extra cost.')
    elif card is None:
        sentence = f'{len(rows):,} local prefixes go out on this line at no extra cost.'
    elif toll is None:
        sentence = (f'{len(rows):,} local prefixes go out on this line at no extra cost, and your plan includes '
                    'calls to other numbers too.')
    else:
        sentence = (f'{len(rows):,} local prefixes go out on this line at no extra cost; other numbers cost {toll} '
                    'on it, and Faxbot sends them the cheapest way.')
    return {'analog': True, 'account': account or 'sip', 'label': found_account.label, 'preset': preset.id,
            'preset_label': preset.label, 'calls_at_once': trunk_calls_at_once(found_account.values),
            'local_prefixes': len(rows), 'imported_at': imported.isoformat(timespec='seconds') + 'Z' if imported else None,
            'sources': sources, 'toll_rate': toll, 'monthly_fee': fee, 'sentence': sentence,
            'save_page_help': LCG_SAVE}
