"""Provider accounts: every account Faxbot sends and receives with, trunks included.

`faxbot delivery providers accounts` is the account list on Delivery setup -> Providers & accounts. The first account of each
provider is the one its own provider page sets up; extra accounts, such as a second Sinch account or a
second trunk, are added here. Secrets are read from a hidden prompt or from standard input, never from
the command line, and Faxbot never shows them again.
"""
import sys

import typer

from .. import state
from ..client import segment
from ..errors import EXIT_FAILURE, EXIT_NOT_FOUND, CliError
from ..output import money_amount
from .rules import parse_money

accounts = typer.Typer(help='The provider accounts Faxbot sends and receives with, trunks included: add one, switch '
                            'one off, or choose the defaults.', no_args_is_help=True)

HEALTH = {'ready': 'Ready', 'not_set_up': 'Not set up', 'off': 'Turned off', 'waiting': 'Waiting for the first fax',
          'failing': 'Failing', 'spending_limit': 'Spending limit reached'}


def load(api):
    return api.get('/admin/providers/accounts')


def find(current, key):
    for account in current.get('accounts') or []:
        if account['key'] == key or account['label'].strip().casefold() == key.strip().casefold():
            return account
    raise CliError(f"No account is called '{key}'. See 'faxbot delivery providers accounts list'.", EXIT_NOT_FOUND)


def provider_kind(current, provider):
    for kind in current.get('providers') or []:
        if kind['id'] == provider or kind['label'].strip().casefold() == provider.strip().casefold():
            return kind
    names = ', '.join(kind['id'] for kind in current.get('providers') or [])
    raise CliError(f"Faxbot has no provider called '{provider}'. Choose one of: {names}.", EXIT_NOT_FOUND)


def limits_text(limits):
    parts = []
    if (limits or {}).get('at_once'):
        parts.append(f"{limits['at_once']} at once")
    if (limits or {}).get('calls_per_second'):
        parts.append(f"{limits['calls_per_second']} calls a second")
    if (limits or {}).get('daily_limit'):
        parts.append(f"{money_amount(limits['daily_limit'])} a day")
    return ', '.join(parts) or 'No limits'


def roles_text(account, current):
    """What an account does, in the console's words: "Sends (default) and receives"."""
    sends = 'Sends' + (' (default)' if current.get('default_sending') == account['key'] else '')
    receives = 'receives' + (' (default)' if current.get('default_receiving') == account['key'] else '')
    if account.get('sends') and account.get('receives'):
        return f'{sends} and {receives}'
    if account.get('sends'):
        return sends
    if account.get('receives'):
        return receives[0].upper() + receives[1:]
    return 'Neither sends nor receives'


def site_name(current, key):
    if not key:
        return '-'
    return next((site['name'] for site in current.get('sites') or [] if site['key'] == key), key)


@accounts.command('list')
def accounts_list():
    """List every provider account, what it does and whether it is ready."""
    current = load(state.api())
    state.out().result(current, lambda out: out.table(
        ['Account', 'Key', 'Provider', 'Site', 'Does', 'On', 'Health', 'Numbers'],
        [[item['label'], item['key'], provider_label(current, item['provider']), site_name(current, item.get('site')),
          roles_text(item, current), 'on' if item.get('enabled') else 'off',
          HEALTH.get(item['health']['state'], item['health']['state']), ', '.join(item.get('numbers') or []) or '-']
         for item in current.get('accounts') or []], empty='No provider accounts yet. Set one up with faxbot admin '
                                                           'setup.'))


def provider_label(current, provider):
    return next((kind['label'] for kind in current.get('providers') or [] if kind['id'] == provider), provider)


def account_fields(account, current):
    kind = next((kind for kind in current.get('providers') or [] if kind['id'] == account['provider']), None)
    pairs = [('Account', account['label']), ('Key', account['key']),
             ('Provider', provider_label(current, account['provider'])), ('Site', site_name(current, account.get('site'))),
             ('Does', roles_text(account, current)), ('On', 'yes' if account.get('enabled') else 'no'),
             ('Health', account['health']['sentence']), ('Numbers', ', '.join(account.get('numbers') or []) or '-'),
             ('Limits', limits_text(account.get('limits')))]
    if account.get('webhook_address'):
        pairs.append(('Give your provider this address', account['webhook_address']))
    for field in (kind or {}).get('fields') or []:
        if field.get('secret'):
            pairs.append((field['label'], 'Set' if field['name'] in (account.get('secrets_set') or []) else 'Not set'))
        else:
            pairs.append((field['label'], account.get('settings', {}).get(field['name'])))
    return pairs


@accounts.command('show')
def accounts_show(key: str = typer.Argument(..., metavar='KEY', help="The account's key or name.")):
    """Show one account: its settings, its numbers and the address to give your provider."""
    current = load(state.api())
    account = find(current, key)
    state.out().result(account, lambda out: out.fields(account_fields(account, current)))


def _settings(pairs):
    settings = {}
    for pair in pairs or []:
        name, sep, value = pair.partition('=')
        if not sep or not name.strip():
            raise CliError('Write each setting as NAME=VALUE.')
        settings[name.strip()] = value.strip()
    return settings


def plain_settings(kind, pairs):
    """--setting values, refusing a secret: a secret on the command line stays in the shell's history."""
    settings = _settings(pairs)
    fields = {field['name']: field for field in kind.get('fields') or []}
    for name in settings:
        if fields.get(name, {}).get('secret'):
            raise CliError(f"{fields[name]['label']} is a secret, so Faxbot asks for it instead of reading it from the "
                           'command line. Leave it out, or give it with --secrets-from-stdin.')
        if fields and name not in fields:
            known = ', '.join(sorted(field for field, spec in fields.items() if not spec.get('secret'))) or 'none'
            raise CliError(f"{kind['label']} has no setting called {name}. Its settings are: {known}.")
    return settings


def read_secrets(kind, *, from_stdin, only=None):
    """Secret values for a provider's secret fields, from standard input (NAME=VALUE lines) or a hidden prompt."""
    fields = [field for field in kind.get('fields') or [] if field.get('secret') and (only is None or field['name'] in only)]
    if not fields:
        return {}
    if from_stdin:
        given = _settings([line for line in sys.stdin.read().splitlines() if line.strip()])
        known = {field['name'] for field in fields}
        unknown = sorted(set(given) - known)
        if unknown:
            raise CliError(f"{kind['label']} has no secret called {', '.join(unknown)}. Its secrets are: "
                           f"{', '.join(sorted(known))}.")
        return given
    secrets = {}
    for field in fields:
        value = typer.prompt(field['label'], hide_input=True, default='' if not field.get('required') else None,
                             show_default=False)
        if value:
            secrets[field['name']] = value
    return secrets


def _limits(at_once, calls_per_second, daily_limit, existing=None):
    limits = dict(existing or {'at_once': None, 'calls_per_second': None, 'daily_limit': None})
    if at_once is not None:
        limits['at_once'] = at_once or None
    if calls_per_second is not None:
        limits['calls_per_second'] = calls_per_second or None
    if daily_limit is not None:
        limits['daily_limit'] = None if daily_limit.strip().casefold() in {'none', 'off', '0'} else parse_money(
            daily_limit, 'daily spending limit')
    return limits


SITE = typer.Option(None, '--site', help='The site its calls start from (see faxbot delivery rules sites).')
NUMBER = typer.Option(None, '--number', help='A fax number this account receives on (repeat it).')
AT_ONCE = typer.Option(None, '--at-once', min=0, help='Faxes at once on this account, or lines at once on a trunk; 0 '
                                                       'for no limit.')
PER_SECOND = typer.Option(None, '--calls-per-second', min=0, help='Trunks: calls started each second; 0 for no limit.')
DAILY = typer.Option(None, '--daily-limit', metavar='AMOUNT',
                     help='Stop using this account for the day once it has cost this much; none for no limit.')
SETTING = typer.Option(None, '--setting', metavar='NAME=VALUE',
                       help="A provider setting that is not secret (repeat it). 'show' lists them.")
STDIN = typer.Option(False, '--secrets-from-stdin',
                     help='Read secrets as NAME=VALUE lines from standard input instead of asking for each one.')


@accounts.command('add')
def accounts_add(provider: str = typer.Option(..., '--provider', help='The provider, such as sinch or sip for a trunk.'),
                 key: str = typer.Option(..., '--key', help='A short key for rules to name it, such as sinch-uk.'),
                 label: str = typer.Option(None, '--label', help='Its name, such as "Sinch (UK)".'),
                 site: str = SITE,
                 sends: bool = typer.Option(True, '--sends/--no-sends', help='Faxbot may send faxes by it.'),
                 receives: bool = typer.Option(None, '--receives/--no-receives',
                                               help='Faxbot receives faxes on it. On when the provider can receive.'),
                 number: list[str] = NUMBER, at_once: int = AT_ONCE, calls_per_second: int = PER_SECOND,
                 daily_limit: str = DAILY, setting: list[str] = SETTING, secrets_from_stdin: bool = STDIN):
    """Add another provider account, such as a second Sinch account or a second trunk. Asks for its secrets."""
    api = state.api()
    current = load(api)
    kind = provider_kind(current, provider)
    settings = plain_settings(kind, setting)
    missing = [field['label'] for field in kind.get('fields') or []
               if field.get('required') and not field.get('secret') and field['name'] not in settings]
    if missing:
        raise CliError(f"{kind['label']} needs these settings too: {', '.join(missing)}. Give each with --setting "
                       'NAME=VALUE.')
    if receives and not kind.get('supports_inbound'):
        raise CliError(f"{kind['label']} cannot receive faxes, so leave out --receives.")
    body = {
        'key': key, 'provider': kind['id'], 'label': label or kind['label'], 'site': site, 'sends': sends,
        'receives': kind.get('supports_inbound', False) if receives is None else receives,
        'numbers': list(number or []), 'limits': _limits(at_once, calls_per_second, daily_limit),
        'settings': settings, 'credentials': read_secrets(kind, from_stdin=secrets_from_stdin),
        'expected_generation': current['generation'],
    }
    result = api.post('/admin/providers/accounts', json=body)
    added = next((item for item in result.get('accounts') or [] if item['key'] == key), None)

    def human(out):
        out.line(f"Account {body['label']} added.")
        if added and added.get('webhook_address'):
            out.line(f"Give {kind['label']} this address for received faxes: {added['webhook_address']}")
    state.out().result(result, human)


@accounts.command('update')
def accounts_update(key: str = typer.Argument(..., metavar='KEY', help="The account's key or name."),
                    label: str = typer.Option(None, '--label', help='A new name.'), site: str = SITE,
                    no_site: bool = typer.Option(False, '--no-site', help='Take it out of its site.'),
                    sends: bool = typer.Option(None, '--sends/--no-sends', help='Faxbot may send faxes by it.'),
                    receives: bool = typer.Option(None, '--receives/--no-receives', help='Faxbot receives faxes on it.'),
                    number: list[str] = typer.Option(None, '--number',
                                                     help='Replace its fax numbers with these (repeat it).'),
                    at_once: int = AT_ONCE, calls_per_second: int = PER_SECOND, daily_limit: str = DAILY,
                    setting: list[str] = SETTING,
                    secret: list[str] = typer.Option(None, '--secret', metavar='NAME',
                                                     help='A secret to change; Faxbot asks for its new value.'),
                    secrets_from_stdin: bool = STDIN):
    """Change an extra account. The first account of a provider is changed on its own provider settings."""
    api = state.api()
    current = load(api)
    account = find(current, key)
    patch = {}
    if label:
        patch['label'] = label
    if site or no_site:
        patch['site'] = None if no_site else site
    if sends is not None:
        patch['sends'] = sends
    if receives is not None:
        patch['receives'] = receives
    if number:
        patch['numbers'] = list(number)
    if at_once is not None or calls_per_second is not None or daily_limit is not None:
        patch['limits'] = _limits(at_once, calls_per_second, daily_limit, account.get('limits'))
    if setting:
        patch['settings'] = {**(account.get('settings') or {}),
                             **plain_settings(provider_kind(current, account['provider']), setting)}
    if secret or secrets_from_stdin:
        kind = provider_kind(current, account['provider'])
        patch['credentials'] = read_secrets(kind, from_stdin=secrets_from_stdin, only=set(secret) if secret else None)
    if not patch:
        raise CliError('Nothing to change. Give at least one option.')
    if patch.get('sends') is False and current.get('default_sending') == account['key']:
        raise CliError(f"{account['label']} is the default sending account. Choose another first with 'faxbot "
                       "delivery providers accounts default-sending KEY'.")
    _patch(api, current, account, patch, f"Account {patch.get('label') or account['label']} saved.")


def _patch(api, current, account, patch, message):
    result = api.patch('/admin/providers/accounts/' + segment(account['key']),
                       json={**patch, 'expected_generation': current['generation']})
    state.out().result(result, lambda out: out.line(message))


@accounts.command('enable')
def accounts_enable(key: str = typer.Argument(..., metavar='KEY', help="The account's key or name.")):
    """Switch an account on. Faxbot uses it for new attempts at once."""
    api = state.api()
    current = load(api)
    account = find(current, key)
    _patch(api, current, account, {'enabled': True}, f"{account['label']} is on.")


@accounts.command('disable')
def accounts_disable(key: str = typer.Argument(..., metavar='KEY', help="The account's key or name.")):
    """Switch an account off. Waiting faxes go by other accounts their rules allow, or wait; none is sent twice."""
    api = state.api()
    current = load(api)
    account = find(current, key)
    if current.get('default_sending') == account['key']:
        raise CliError(f"{account['label']} is the default sending account. Choose another first with 'faxbot "
                       "delivery providers accounts default-sending KEY'.", EXIT_FAILURE)
    _patch(api, current, account, {'enabled': False},
           f"{account['label']} is off. Faxes already sent by it are not affected.")


@accounts.command('default-sending')
def accounts_default_sending(key: str = typer.Argument(..., metavar='KEY', help="The account's key or name.")):
    """Choose the account Faxbot sends by when no rule says otherwise."""
    api = state.api()
    current = load(api)
    account = find(current, key)
    if not account.get('sends'):
        raise CliError(f"{account['label']} does not send faxes. Turn sending on first with --sends.")
    _patch(api, current, account, {'default_sending': True}, f"Faxbot now sends by {account['label']} unless a rule "
                                                             'says otherwise.')


@accounts.command('default-receiving')
def accounts_default_receiving(key: str = typer.Argument(..., metavar='KEY', help="The account's key or name.")):
    """Choose the account whose notifications arrive at the provider's original address."""
    api = state.api()
    current = load(api)
    account = find(current, key)
    if not account.get('receives'):
        raise CliError(f"{account['label']} does not receive faxes. Turn receiving on first with --receives.")
    _patch(api, current, account, {'default_receiving': True}, f"{account['label']} is the default receiving account.")


@accounts.command('health')
def accounts_health(key: str = typer.Argument(None, metavar='KEY', help="One account's key or name; all when left out.")):
    """Whether each account is ready, and what to do when it is not."""
    api = state.api()
    current = load(api)
    if key is None:
        state.out().result(current, lambda out: out.table(
            ['Account', 'Health', 'What to do'],
            [[item['label'], HEALTH.get(item['health']['state'], item['health']['state']), item['health']['sentence']]
             for item in current.get('accounts') or []], empty='No provider accounts yet.'))
        return
    account = find(current, key)
    result = api.get('/admin/providers/accounts/' + segment(account['key']) + '/health')

    def human(out):
        out.line(f"{account['label']}: {HEALTH.get(result['state'], result['state'])}. {result['sentence']}")
        for detail in result.get('details') or []:
            out.line(detail)
    state.out().result(result, human)


# -- prices by where calls start (faxbot savings fax --to and costs rate-cards) ----------------------------

def quote_command(to: str = typer.Option(..., '--to', metavar='NUMBER', help='The fax number to price.'),
                  pages: int = typer.Option(1, '--pages', min=1, max=1000, help='Pages in the fax.'),
                  from_site: str = typer.Option(None, '--from-site', metavar='SITE',
                                                help="Price calls from this site's accounts first, such as leeds.")):
    """What one fax would cost by each account your rules allow, from where its calls start. Nothing is sent."""
    result = state.api().get('/routing/quote', params={'to': to, 'pages': pages, 'site': from_site})
    state.out().result(result, lambda out: out.table(
        ['Account', 'Calls from', 'Estimate', 'How it is priced'],
        [[item['label'], item.get('origin_label') or '-', _estimate_text(item), item.get('sentence') or '-']
         for item in result.get('quotes') or []], empty='No account your rules allow can send to this number.'))


def _estimate_text(item):
    """'About $0.07', or the server's words for a plan's fax ('In your plan') or an unknown price; never $0.00."""
    if item.get('estimate_text'):
        return item['estimate_text']
    return f"About {money_amount(item['estimate'])}" if item.get('estimate') else 'Not priced yet'


def billing_text(row):
    increment = 'whole minutes' if row['billing_increment_seconds'] == 60 else f"{row['billing_increment_seconds']}-second steps"
    return f"{increment}, at least {row['minimum_seconds']} seconds" if row.get('minimum_seconds') else increment


def origin_rows(out, card):
    """A rate card's prices by where calls start, as the console's Prices & plans lists them."""
    rows = card.get('rows') or []
    if not rows:
        return
    out.line(f"{card['label']}: prices by where calls start")

    def price(row):
        parts = [f"{money_amount({'currency': row['currency'], 'amount': row[key]})} a {unit}"
                 for key, unit in (('per_minute', 'minute'), ('per_page', 'page'), ('per_call', 'call'))
                 if float(row.get(key) or 0) > 0]
        return ', '.join(parts) or 'No charge'
    out.table(['Calls from', 'To numbers starting with', 'Price', 'Billed in', 'Source'],
              [[row['origin_label'], row['destination_prefix'], price(row), billing_text(row),
                (row.get('source_url') or 'Entered here') + (f", read on {row['captured_on']}" if row.get('captured_on') else '')]
               for row in rows])
