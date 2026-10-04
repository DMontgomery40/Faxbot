"""Starting rate cards from a shipped JSON file, used only while the table is empty.

The file lists advertised carrier prices with their source and date, and under
``plans`` flat monthly plans (a ``monthly_fee`` with nothing charged per fax).
Entries that do not validate are skipped; operators edit the cards in the console.
"""
from datetime import datetime
import json
from pathlib import Path
import re

from ..config_paths import bundled_config_dir
from .costs import InvalidRateCard, RateCard, parse_amount


_INCREMENTS = {'whole_minute': 60, 'per_minute': 60, 'minute': 60, 'per_second': 1, 'second': 1,
               'six_second': 6, '6_second': 6}


def default_path():
    return bundled_config_dir() / 'rate_cards.json'


def _increment(entry):
    if type(entry.get('billing_increment_seconds')) is int:
        return entry['billing_increment_seconds']
    rule = str(entry.get('rounding') or entry.get('rounding_rule') or 'whole_minute').strip().lower()
    if rule in _INCREMENTS:
        return _INCREMENTS[rule]
    match = re.fullmatch(r'(\d{1,4})[_ -]?s(?:ec(?:ond)?s?)?', rule)
    return int(match[1]) if match else 60


def _date(value):
    text = str(value or '')[:10]
    return datetime.strptime(text, '%Y-%m-%d')


def _entries(document):
    if isinstance(document, list):
        return document
    if isinstance(document, dict):
        for key in ('cards', 'rate_cards', 'presets', 'carriers'):
            value = document.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                return [{'provider_id': name, **entry} for name, entry in value.items() if isinstance(entry, dict)]
    return []


def load_cards(path=None):
    path = Path(path) if path is not None else default_path()
    try:
        document = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    cards = []
    plans = document.get('plans') if isinstance(document, dict) else None
    for entry in _entries(document) + (plans if isinstance(plans, list) else []):
        if not isinstance(entry, dict):
            continue
        try:
            provider = str(entry.get('provider_id') or entry.get('provider') or entry.get('id') or '').strip().lower()
            cards.append(RateCard(
                None, provider, entry.get('direction', 'outbound'),
                str(entry.get('label') or entry.get('name') or provider)[:100], str(entry.get('currency', 'USD')).upper(),
                parse_amount(str(entry.get('per_minute', '0'))), parse_amount(str(entry.get('per_page', '0'))),
                parse_amount(str(entry.get('per_call', entry.get('setup', '0')))), _increment(entry),
                int(entry.get('minimum_seconds', 0)), entry.get('source_url') or entry.get('source') or None,
                _date(entry.get('captured_on') or entry.get('advertised_on')),
                None if entry.get('monthly_fee') in (None, '') else parse_amount(str(entry['monthly_fee']), whole_digits=4)))
        except (InvalidRateCard, ValueError, TypeError):
            continue
    return cards
