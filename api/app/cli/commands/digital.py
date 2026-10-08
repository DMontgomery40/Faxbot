"""Direct messages and FHIR on the command line: the accounts (Providers) and each recipient's addresses (Recipients).

`faxbot providers digital` manages HISP accounts and FHIR clients. Secrets (passwords, private keys) are read
from a hidden prompt, from standard input, or from a file, never from the command line, and Faxbot never shows
them again. `faxbot recipients digital` puts a recipient's Direct address or FHIR endpoint on file, confirms or
withdraws it, and asks the NPI registry for suggestions. Faxbot uses an address only once it is confirmed.
"""
import json
from pathlib import Path
import sys

import typer

from .. import state
from ..client import segment
from ..errors import EXIT_NOT_FOUND, CliError
from ..output import local_time

accounts = typer.Typer(help='Direct messages and FHIR: the HISP account and FHIR clients Faxbot delivers faxes with '
                            'instead of calling, when a recipient can take them.', no_args_is_help=True)
recipients = typer.Typer(help="A recipient's Direct address or FHIR endpoint: put one on file, confirm it, withdraw "
                              'it, or look one up in the NPI registry.', no_args_is_help=True)

KINDS = {'hisp': 'Direct messages (HISP)', 'fhir': 'FHIR client'}
HEALTH = {'ready': 'Ready', 'not_set_up': 'Not set up', 'off': 'Turned off'}
STATES = {'suggested': 'Suggested', 'confirmed': 'Confirmed', 'withdrawn': 'Withdrawn', 'dismissed': 'Dismissed'}
SOURCES = {'entered': 'You entered it', 'nppes': 'NPI registry'}
MESSAGE_STATES = {'sending': 'Sending', 'submitted': 'Waiting for the recipient', 'processed': 'Accepted by their HISP',
                  'dispatched': 'Delivered', 'delivered': 'Delivered', 'failed': 'Not delivered',
                  'uncertain': 'Not known', 'refused': 'Not sent', 'filed': 'Filed', 'not_filed': 'Not filed'}


def load(api):
    return api.get('/digital/accounts')


def find(current, key):
    for account in current.get('accounts') or []:
        if account['key'] == key or account['label'].strip().casefold() == key.strip().casefold():
            return account
    raise CliError(f"No Direct or FHIR account is called '{key}'. See 'faxbot providers digital list'.",
                   EXIT_NOT_FOUND)


def kind_of(current, kind):
    for item in current.get('kinds') or []:
        if item['id'] == kind:
            return item
    raise CliError("Choose --kind hisp for a HISP account or --kind fhir for a FHIR client.")


def _pairs(values, what='setting'):
    found = {}
    for pair in values or []:
        name, sep, value = pair.partition('=')
        if not sep or not name.strip():
            raise CliError(f'Write each {what} as NAME=VALUE.')
        found[name.strip()] = value.strip()
    return found


def _settings(kind, setting, files):
    """Plain settings from --setting and --file; a secret given with --setting is refused (shell history)."""
    fields = {field['name']: field for field in kind['fields']}
    settings, secrets = {}, {}
    for name, value in _pairs(setting).items():
        field = fields.get(name)
        if field is None:
            raise CliError(f"{kind['label']} has no setting called {name}. Its settings are: "
                           f"{', '.join(sorted(item for item, spec in fields.items() if not spec['secret']))}.")
        if field['secret']:
            raise CliError(f"{field['label']} is a secret, so Faxbot does not read it from the command line. Give it "
                           f'with --file {name}=PATH, --secret {name} or --secrets-from-stdin.')
        settings[name] = value
    for name, path in _pairs(files, 'file').items():
        field = fields.get(name)
        if field is None:
            raise CliError(f"{kind['label']} has no setting called {name}.")
        try:
            text = Path(path).expanduser().read_text(encoding='ascii')
        except (OSError, UnicodeDecodeError):
            raise CliError(f'Faxbot could not read {path} as text.') from None
        (secrets if field['secret'] else settings)[name] = text
    return settings, secrets


def _secrets(kind, names, from_stdin):
    fields = {field['name']: field for field in kind['fields'] if field['secret']}
    if from_stdin:
        given = _pairs([line for line in sys.stdin.read().splitlines() if line.strip()])
        unknown = sorted(set(given) - set(fields))
        if unknown:
            raise CliError(f"{kind['label']} has no secret called {', '.join(unknown)}. Its secrets are: "
                           f"{', '.join(sorted(fields))}.")
        return given
    found = {}
    for name in names or []:
        if name not in fields:
            raise CliError(f"{kind['label']} has no secret called {name}. Its secrets are: {', '.join(sorted(fields))}.")
        found[name] = typer.prompt(fields[name]['label'], hide_input=True)
    return found


def account_fields(account, current):
    kind = kind_of(current, account['provider'])
    pairs = [('Account', account['label']), ('Key', account['key']), ('Kind', KINDS[account['provider']]),
             ('On', 'yes' if account['enabled'] else 'no'), ('Health', account['health']['sentence']),
             ('Plan', account.get('plan') or '-')]
    if account.get('certificate'):
        pairs.append(('Your certificate', account['certificate']['sentence']))
    if account['provider'] == 'hisp':
        bundle = account.get('trust_bundle')
        pairs.append(('Trust bundle', f"{bundle['anchors']} authorities, loaded {local_time(bundle['loaded_at'])}"
                      if bundle else 'Not loaded'))
    for field in kind['fields']:
        if field['kind'] == 'pem' and not field['secret']:
            continue
        if field['secret']:
            pairs.append((field['label'], 'Set' if field['name'] in account['secrets_set'] else 'Not set'))
        else:
            value = account['settings'].get(field['name'])
            pairs.append((field['label'], {True: 'yes', False: 'no'}.get(value, value) if value is not None else '-'))
    if account['provider'] == 'fhir' and account.get('public_keys'):
        pairs.append(('Public key set', f"{state.api().url}/digital/jwks/{segment(account['key'])}"))
    return pairs


FILE = typer.Option(None, '--file', metavar='NAME=PATH',
                    help='Read a setting or secret from a file, such as certificate=cert.pem or private_key=key.pem.')
SETTING = typer.Option(None, '--setting', metavar='NAME=VALUE', help="A setting that is not secret (repeat it). "
                                                                     "'show' lists them.")
SECRET = typer.Option(None, '--secret', metavar='NAME', help='A secret to enter; Faxbot asks for its value.')
STDIN = typer.Option(False, '--secrets-from-stdin',
                     help='Read secrets as NAME=VALUE lines from standard input instead of asking for each one.')


@accounts.command('list')
def accounts_list():
    """List the HISP account and FHIR clients and whether each is ready."""
    current = load(state.api())
    state.out().result(current, lambda out: out.table(
        ['Account', 'Key', 'Kind', 'On', 'Health', 'Plan'],
        [[item['label'], item['key'], KINDS[item['provider']], 'on' if item['enabled'] else 'off',
          HEALTH.get(item['health']['state'], item['health']['state']), item.get('plan') or '-']
         for item in current.get('accounts') or []],
        empty="No Direct or FHIR account yet. Add one with 'faxbot providers digital add'."))


@accounts.command('show')
def accounts_show(key: str = typer.Argument(..., metavar='KEY', help="The account's key or name.")):
    """Show one account: its settings, its plan and what it still needs."""
    current = load(state.api())
    account = find(current, key)
    state.out().result(account, lambda out: out.fields(account_fields(account, current)))


@accounts.command('add')
def accounts_add(kind: str = typer.Option(..., '--kind', help='hisp for Direct messages, fhir for a FHIR client.'),
                 key: str = typer.Option(..., '--key', help='A short key, such as hisp or fhir-epic.'),
                 label: str = typer.Option(None, '--label', help='Its name, such as "Direct through Inpriva".'),
                 setting: list[str] = SETTING, file: list[str] = FILE, secret: list[str] = SECRET,
                 secrets_from_stdin: bool = STDIN):
    """Add a HISP account or a FHIR client. Asks for the HISP password unless you give it another way."""
    api = state.api()
    current = load(api)
    spec = kind_of(current, kind)
    settings, secrets = _settings(spec, setting, file)
    names = list(secret or [])
    if kind == 'hisp' and 'password' not in secrets and not secrets_from_stdin and 'password' not in names:
        names.append('password')
    secrets.update(_secrets(spec, names, secrets_from_stdin))
    result = api.post('/digital/accounts', json={'key': key, 'provider': kind, 'label': label, 'settings': settings,
                                                 'credentials': secrets,
                                                 'expected_generation': current['generation']})
    added = next((item for item in result['accounts'] if item['key'] == key), None)

    def human(out):
        out.line(f"{added['label'] if added else key} added. {added['health']['sentence'] if added else ''}".strip())
    state.out().result(result, human)


@accounts.command('update')
def accounts_update(key: str = typer.Argument(..., metavar='KEY', help="The account's key or name."),
                    label: str = typer.Option(None, '--label', help='A new name.'),
                    on: bool = typer.Option(None, '--on/--off', help='Turn it on or off.'),
                    setting: list[str] = SETTING, file: list[str] = FILE, secret: list[str] = SECRET,
                    secrets_from_stdin: bool = STDIN):
    """Change an account's name, settings or secrets, or turn it on or off."""
    api = state.api()
    current = load(api)
    account = find(current, key)
    spec = kind_of(current, account['provider'])
    settings, secrets = _settings(spec, setting, file)
    secrets.update(_secrets(spec, secret, secrets_from_stdin))
    patch = {'expected_generation': current['generation']}
    if label:
        patch['label'] = label
    if on is not None:
        patch['enabled'] = on
    if settings:
        patch['settings'] = settings
    if secrets:
        patch['credentials'] = secrets
    if len(patch) == 1:
        raise CliError('Nothing to change. Give --label, --on/--off, --setting, --file or --secret.')
    result = api.patch(f"/digital/accounts/{segment(account['key'])}", json=patch)
    changed = find(result, account['key'])
    state.out().result(result, lambda out: out.line(f"{changed['label']} changed. {changed['health']['sentence']}"))


@accounts.command('signing-key')
def accounts_signing_key(key: str = typer.Argument(..., metavar='KEY', help="The FHIR client's key or name."),
                         algorithm: str = typer.Option(None, '--algorithm',
                                                       help='RS384 (an RSA key) or ES384 (an elliptic-curve key).')):
    """Make a new signing key for a FHIR client. Register its public key set with the recipient's system."""
    api = state.api()
    current = load(api)
    account = find(current, key)
    if account['provider'] != 'fhir':
        raise CliError('Only a FHIR client has a signing key.')
    result = api.post(f"/digital/accounts/{segment(account['key'])}/signing-key",
                      json={'algorithm': algorithm, 'expected_generation': current['generation']})

    def human(out):
        out.line('Faxbot made a new signing key. Give the recipient\'s system this public key set address:')
        out.line(f"{api.url}/digital/jwks/{segment(account['key'])}")
    state.out().result(result, human)


@accounts.command('public-keys')
def accounts_public_keys(key: str = typer.Argument(..., metavar='KEY', help="The FHIR client's key or name.")):
    """Print a FHIR client's public key set, for a recipient's system that asks for it pasted in."""
    current = load(state.api())
    account = find(current, key)
    if not account.get('public_keys'):
        raise CliError("This FHIR client has no signing key yet. Make one with 'faxbot providers digital "
                       "signing-key'.")
    state.out().result(account['public_keys'], lambda out: out.line(json.dumps(account['public_keys'], indent=2)))


@accounts.command('trust-bundle')
def accounts_trust_bundle(key: str = typer.Argument(..., metavar='KEY', help="The HISP account's key or name."),
                          url: str = typer.Option(None, '--url', help='The trust bundle\'s web address (https).'),
                          file: Path = typer.Option(None, '--file', help='A trust bundle file (.p7b or PEM).')):
    """Load the trust bundle your HISP belongs to, so Faxbot can check recipients' certificates."""
    import base64
    if bool(url) == bool(file):
        raise CliError('Give either --url or --file.')
    body = {'url': url}
    if file:
        try:
            data = file.expanduser().read_bytes()
        except OSError:
            raise CliError(f'Faxbot could not read {file}.') from None
        body = {'content': data.decode('ascii') if b'-----BEGIN' in data else base64.b64encode(data).decode('ascii')}
    api = state.api()
    account = find(load(api), key)
    result = api.post(f"/digital/accounts/{segment(account['key'])}/trust-bundle", json=body)
    changed = find(result, account['key'])
    state.out().result(result, lambda out: out.line(
        f"Trust bundle loaded: {changed['trust_bundle']['anchors']} authorities. {changed['health']['sentence']}"))


# Recipients -------------------------------------------------------------------------------------------------------

def _recipient(api, number):
    return api.get(f'/digital/recipients/{segment(number)}')


def _address(view, reference):
    found = [item for item in view['addresses']
             if item['id'] == reference or item['address'] == reference.strip().lower().rstrip('/')]
    if len(found) != 1:
        raise CliError(f"No single address matches '{reference}'. See 'faxbot recipients digital show "
                       f"{view['number']}'.", EXIT_NOT_FOUND)
    return found[0]


def show_recipient(out, view):
    out.line(view['sentence'])
    out.table(['What', 'Address', 'State', 'From', 'Added'],
              [[item['kind_label'], item['address'], STATES[item['state']], SOURCES[item['source']],
                local_time(item['created_at'])] for item in view['addresses']],
              empty='No Direct address or FHIR endpoint is on file for this number.')
    if view.get('nppes_sentence'):
        out.line(view['nppes_sentence'])


@recipients.command('show')
def recipients_show(number: str = typer.Argument(..., metavar='NUMBER', help='The fax number.')):
    """Show a recipient's Direct address and FHIR endpoint, and whether Faxbot may use them."""
    view = _recipient(state.api(), number)
    state.out().result(view, lambda out: show_recipient(out, view))


@recipients.command('add')
def recipients_add(number: str = typer.Argument(..., metavar='NUMBER', help='The fax number.'),
                   direct: str = typer.Option(None, '--direct', help='The recipient\'s Direct address.'),
                   fhir: str = typer.Option(None, '--fhir', help='The recipient\'s FHIR server base address (https).'),
                   account: str = typer.Option(None, '--account',
                                               help='The HISP account or FHIR client to use; the first one if left '
                                                    'out.'),
                   organization: str = typer.Option(None, '--organization', help='Who it belongs to.'),
                   confirm: bool = typer.Option(False, '--confirm',
                                                help='Confirm it now, so faxes may go this way.'),
                   note: str = typer.Option(None, '--note', help='Where the recipient gave it, for the history.')):
    """Put a recipient's Direct address or FHIR endpoint on file. Faxbot uses it only once confirmed."""
    if bool(direct) == bool(fhir):
        raise CliError('Give either --direct or --fhir.')
    api = state.api()
    view = api.post(f'/digital/recipients/{segment(number)}', json={
        'kind': 'direct' if direct else 'fhir', 'address': direct or fhir, 'account_key': account,
        'organization': organization, 'confirm': confirm, 'note': note})
    state.out().result(view, lambda out: show_recipient(out, view))


def _change(number, reference, action, note):
    api = state.api()
    item = _address(_recipient(api, number), reference)
    view = api.post(f"/digital/recipients/{segment(number)}/addresses/{segment(item['id'])}",
                    json={'action': action, 'note': note})
    state.out().result(view, lambda out: show_recipient(out, view))


ADDRESS = typer.Argument(..., metavar='ADDRESS', help='The Direct address or FHIR address, as shown.')
NOTE = typer.Option(None, '--note', help='Why, for the history.')


@recipients.command('confirm')
def recipients_confirm(number: str = typer.Argument(..., metavar='NUMBER', help='The fax number.'),
                       address: str = ADDRESS, note: str = NOTE):
    """Confirm an address, so faxes to this number may go that way when it is the better route."""
    _change(number, address, 'confirm', note)


@recipients.command('withdraw')
def recipients_withdraw(number: str = typer.Argument(..., metavar='NUMBER', help='The fax number.'),
                        address: str = ADDRESS, note: str = NOTE):
    """Stop using a confirmed address; faxes go by fax again."""
    _change(number, address, 'withdraw', note)


@recipients.command('dismiss')
def recipients_dismiss(number: str = typer.Argument(..., metavar='NUMBER', help='The fax number.'),
                       address: str = ADDRESS, note: str = NOTE):
    """Dismiss a suggested address you do not want."""
    _change(number, address, 'dismiss', note)


@recipients.command('nppes')
def recipients_nppes(number: str = typer.Argument(..., metavar='NUMBER', help='The fax number.'),
                     npi: str = typer.Option(..., '--npi', help='The recipient\'s ten-digit NPI.')):
    """Ask the NPI registry for this provider's Direct address and FHIR endpoint, as suggestions to check."""
    view = state.api().post(f'/digital/recipients/{segment(number)}/nppes', json={'npi': npi})
    state.out().result(view, lambda out: show_recipient(out, view))


@recipients.command('messages')
def recipients_messages(received: bool = typer.Option(False, '--received', help='Show messages received.'),
                        sent: bool = typer.Option(False, '--sent', help='Show messages sent.'),
                        limit: int = typer.Option(50, '--limit', min=1, max=200, help='How many to show.')):
    """List Direct messages and FHIR documents sent and received, newest first, with what happened to each."""
    direction = 'in' if received and not sent else 'out' if sent and not received else None
    params = {'limit': limit, **({'direction': direction} if direction else {})}
    view = state.api().get('/digital/messages', params=params)
    state.out().result(view, lambda out: out.table(
        ['When', 'Way', 'With', 'State', 'What happened'],
        [[local_time(item['created_at']), 'Sent' if item['direction'] == 'out' else 'Received', item['counterpart'],
          MESSAGE_STATES.get(item['state'], item['state']), item['sentence'] or '-']
         for item in view['messages']], empty='No Direct messages or FHIR documents yet.'))
