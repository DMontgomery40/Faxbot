"""Human output (rich tables, local times) and machine output (--json)."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import sys

from rich.console import Console
from rich.table import Table
from rich.text import Text
from rich import box


def parse_time(value):
    """An API time as an aware datetime. The API sends naive times in UTC."""
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str) and value:
        try:
            moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
    else:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def local_time(value, *, empty='-'):
    """A time in the user's local time zone, for people to read."""
    moment = parse_time(value)
    if moment is None:
        return empty if not value else str(value)
    local = moment.astimezone()
    return local.strftime('%Y-%m-%d %H:%M ') + (local.tzname() or '')


def yes_no(value):
    if value is None:
        return '-'
    return 'yes' if value else 'no'


def text(value, empty='-'):
    if value is None or value == '':
        return empty
    if isinstance(value, bool):
        return yes_no(value)
    if isinstance(value, (list, tuple)):
        return ', '.join(str(item) for item in value) or empty
    return str(value)


# Symbols for the installation's own currency; any other currency keeps its code, as in the console.
SYMBOLS = {'USD': '$', 'CAD': '$', 'AUD': '$', 'NZD': '$', 'SGD': '$', 'HKD': '$', 'GBP': '£', 'EUR': '€',
           'JPY': '¥', 'INR': '₹', 'CHF': 'CHF '}
COUNTRY_CURRENCY = {
    'US': 'USD', 'PR': 'USD', 'CA': 'CAD', 'GB': 'GBP', 'AU': 'AUD', 'NZ': 'NZD', 'SG': 'SGD', 'HK': 'HKD', 'JP': 'JPY',
    'IN': 'INR', 'CH': 'CHF', 'IE': 'EUR', 'DE': 'EUR', 'FR': 'EUR', 'ES': 'EUR', 'IT': 'EUR', 'NL': 'EUR', 'BE': 'EUR',
    'AT': 'EUR', 'PT': 'EUR', 'FI': 'EUR', 'GR': 'EUR', 'LU': 'EUR'}


def home_currency():
    """The installation's currency, from its country; read once per command, US dollars when unknown."""
    from . import state
    from .errors import CliError
    current = state.current()
    if current.home_currency is None:
        try:
            country = (current.api().get('/auth/context').get('send') or {}).get('default_country')
        except (CliError, AttributeError):
            country = None
        current.home_currency = COUNTRY_CURRENCY.get(country or 'US', 'USD')
    return current.home_currency


def money_amount(item, home=None):
    """One amount as the console shows it: "$0.005", "$1.50", or "0.005 EUR" outside the installation's currency.

    Two decimal places, or up to four for amounts under ten cents.
    """
    try:
        amount = Decimal(str(item['amount']))
    except (InvalidOperation, KeyError, TypeError):
        return '-'
    places = 4 if amount != 0 and abs(amount) < Decimal('0.1') else 2
    shown = f'{abs(amount):.{places}f}'
    while places > 2 and shown.endswith('0'):
        shown, places = shown[:-1], places - 1
    sign = '-' if amount < 0 else ''
    currency = item.get('currency') or ''
    symbol = SYMBOLS.get(currency) if currency == (home or home_currency()) else None
    return f'{sign}{symbol}{shown}' if symbol else f'{sign}{shown} {currency}'.strip()


def money(amounts, empty='-'):
    """[{'currency': 'USD', 'amount': '0.07'}] as '$0.07'; several currencies are joined with '+'."""
    if not amounts:
        return empty
    home = home_currency()
    return ' + '.join(money_amount(item, home) for item in amounts)


def cost_amount(cost):
    """A cost column's short amount: what was charged, or the estimate marked as one, as the console shows it."""
    state = (cost or {}).get('state')
    if not cost or state == 'none':
        return '-'
    if state == 'reported':
        return money(cost.get('reported_cost'))
    if state == 'partial':
        return f"{money(cost.get('reported_cost'))} charged so far"
    if state == 'included':
        return 'In your plan'
    if state == 'local':
        return 'No call'
    if state == 'unmatched':
        return 'Unknown'
    return f"{money(cost['estimated_cost'])} estimate" if cost.get('estimated_cost') else 'Not reported yet'


class Output:
    def __init__(self, *, json_mode=False, quiet=False):
        self.json_mode = json_mode
        self.quiet = quiet

    @property
    def console(self):
        # Created per call so a replaced sys.stdout (tests, pipes) is honoured.
        # Names, notes and errors come from the server: never read them as markup or emoji codes.
        return Console(file=sys.stdout, highlight=False, soft_wrap=False, markup=False, emoji=False)

    def json(self, data):
        sys.stdout.write(json.dumps(data, indent=2, default=str, ensure_ascii=False) + '\n')

    def result(self, data, human=None):
        """Print data as JSON with --json, through human(self) otherwise, nothing with --quiet."""
        if self.json_mode:
            self.json(data)
        elif not self.quiet and human is not None:
            human(self)

    def line(self, message=''):
        if not self.quiet and not self.json_mode:
            self.console.print(message, markup=False, highlight=False)

    def secret(self, label, value):
        """A one-time secret. Shown even with --quiet, because it cannot be shown again."""
        if self.json_mode:
            return
        if self.quiet:
            sys.stdout.write(value + '\n')
            return
        self.console.print(f'{label}: {value}', markup=False, highlight=False)

    def table(self, columns, rows, *, title=None, empty='Nothing to show.'):
        if not rows:
            self.line(empty)
            return
        table = Table(title=Text(title) if title else None, box=box.SIMPLE_HEAD, show_lines=False,
                      title_justify='left')
        for column in columns:
            table.add_column(Text(column), overflow='fold')
        for row in rows:
            table.add_row(*[Text(text(cell)) for cell in row])
        self.console.print(table)

    def fields(self, pairs, *, title=None):
        """Label/value pairs as a two-column table."""
        table = Table(title=title, box=None, show_header=False, title_justify='left', pad_edge=False)
        table.add_column('Field', style='bold', no_wrap=True)
        table.add_column('Value', overflow='fold')
        for label, value in pairs:
            table.add_row(Text(label), Text(text(value)))
        self.console.print(table)


def error(message, *, json_mode=False, data=None):
    if json_mode:
        sys.stdout.write(json.dumps({'error': data or {'message': message}}, indent=2, default=str) + '\n')
    else:
        sys.stderr.write(message + '\n')
