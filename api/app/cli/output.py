"""Human output (rich tables, local times) and machine output (--json)."""
from datetime import datetime, timezone
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


def money(amounts):
    """[{'currency': 'USD', 'amount': '0.07'}] as '0.07 USD'."""
    if not amounts:
        return '-'
    return ', '.join(f"{item['amount']} {item['currency']}" for item in amounts)


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
