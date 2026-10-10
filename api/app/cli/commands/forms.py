"""Registered forms: import them, fill and preview them, and send them to partners or as faxes."""
import base64
import json
from pathlib import Path

import typer

from .. import state
from ..client import segment
from ..errors import CliError
from ..output import local_time, text
from .fax import save_document

forms = typer.Typer(help='Registered forms: import a fillable PDF or a template, fill it in, preview it and send it. '
                         'A partner running Faxbot gets only the filled-in values and draws identical pages itself.',
                    no_args_is_help=True)


def _forms(api):
    return api.get('/forms')['forms']


def _form(api, reference):
    items = _forms(api)
    matches = [item for item in items if item['id'] == reference or item['name'].casefold() == reference.casefold()]
    if len(matches) != 1:
        raise CliError(f"No single form matches '{reference}'. See 'faxbot faxes forms list'.")
    return matches[0]


def _version(api, reference, number=None):
    form = _form(api, reference)
    versions = form['versions']
    if number is None:
        return form, versions[-1]
    found = [version for version in versions if version['number'] == number]
    if not found:
        raise CliError(f"{form['name']} has no version {number}. See 'faxbot faxes forms show {form['name']}'.")
    return form, found[0]


def _values(value, values_file, signature):
    filled = {}
    if values_file is not None:
        try:
            filled.update(json.loads(values_file.read_text(encoding='utf-8')))
        except (OSError, ValueError):
            raise CliError(f'Cannot read the values in {values_file}; it must be a JSON object of field names.') from None
    for item in value or []:
        name, separator, entry = item.partition('=')
        if not separator or not name:
            raise CliError(f"Write each value as name=value, not '{item}'.")
        filled[name] = entry
    for item in signature or []:
        name, separator, path = item.partition('=')
        if not separator:
            raise CliError(f"Write each signature as name=picture.png, not '{item}'.")
        try:
            filled[name] = {'picture': base64.b64encode(Path(path).read_bytes()).decode('ascii')}
        except OSError:
            raise CliError(f'Cannot read {path}.') from None
    return filled


VALUE = typer.Option(None, '--value', '-v', metavar='NAME=VALUE',
                     help='A field value, for example --value patient="Ann Example". Dates are year-month-day, '
                          'checkboxes yes or no. Repeat for each field.')
VALUES_FILE = typer.Option(None, '--values', exists=True, dir_okay=False, readable=True,
                           help='A JSON file of field names and values.')
SIGNATURE = typer.Option(None, '--signature', metavar='NAME=PICTURE',
                         help='A signature field and its picture (PNG, JPEG or GIF), for example --signature '
                              'signed=signature.png.')
VERSION = typer.Option(None, '--version', min=1, help='The version to use. Default: the newest.')


@forms.command('list')
def forms_list():
    """List your registered forms and their versions."""
    items = _forms(state.api())
    rows = [[item['name'], version['number'], version['pages'], version['fields'], version['source_text'],
             local_time(version['created_at'])] for item in items for version in item['versions']]
    state.out().result(items, lambda out: out.table(['Form', 'Version', 'Pages', 'Fields', 'Where it came from',
                                                     'Added'], rows, empty='No registered forms yet.'))


@forms.command('import')
def forms_import(file: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True,
                                             help='A fillable PDF, or a PDF or SVG template.'),
                 name: str = typer.Option(None, '--name', help='The name for a new form.'),
                 form: str = typer.Option(None, '--form', help='Add this as the next version of an existing form '
                                                               '(name). Earlier versions never change.'),
                 positions: Path = typer.Option(None, '--positions', exists=True, dir_okay=False, readable=True,
                                                help='A field-position file (JSON) placing each field on a template '
                                                     'that has no fillable fields.')):
    """Import a form from a fillable PDF, or from a template and a field-position file."""
    if (name is None) == (form is None):
        raise CliError('Give either --name for a new form or --form for a new version of an existing one.')
    api = state.api()
    media = 'image/svg+xml' if file.suffix.lower() == '.svg' else 'application/pdf'
    with file.open('rb') as handle:
        files = {'file': (file.name, handle.read(), media)}
    if positions is not None:
        files['positions'] = (positions.name, positions.read_bytes(), 'application/json')
    if form is None:
        result = api.post('/forms', data={'name': name}, files=files)
    else:
        result = api.post(f"/forms/{segment(_form(api, form)['id'])}/versions", files=files)
    version = result['version']
    state.out().result(result, lambda out: out.line(
        f"{result['message']} {version['pages']} page{'s' if version['pages'] != 1 else ''}, "
        f"{version['fields']} field{'s' if version['fields'] != 1 else ''}."))


@forms.command('show')
def forms_show(form: str = typer.Argument(..., help='Form name.'), version: int = VERSION):
    """Show a form version's fields: their names, types and choices."""
    api = state.api()
    _, chosen = _version(api, form, version)
    detail = api.get(f"/forms/versions/{segment(chosen['id'])}")

    def human(out):
        out.fields([('Form', detail['form_name']), ('Version', detail['number']), ('Pages', detail['pages']),
                    ('Where it came from', detail['source_text']), ('Added', local_time(detail['created_at']))])
        out.table(['Field', 'Name to use', 'Type', 'Page', 'Required', 'Choices or format'],
                  [[item['label'], item['name'], item['type_text'], item['page'], text(item['required']),
                    ', '.join(item.get('options') or []) or item.get('format') or
                    (f"{item['decimals']} decimal places" if 'decimals' in item else '')]
                   for item in detail['field_list']])
    state.out().result(detail, human)


@forms.command('render')
def forms_render(form: str = typer.Argument(..., help='Form name.'), version: int = VERSION,
                 value: list[str] = VALUE, values_file: Path = VALUES_FILE, signature: list[str] = SIGNATURE,
                 output: str = typer.Option(None, '--output', '-o', help="File to write. Use '-' for standard output."),
                 picture: bool = typer.Option(False, '--png', help='Save the first page as a PNG picture instead of a '
                                                                   'PDF.'),
                 force: bool = typer.Option(False, '--force', help='Replace the file if it exists.')):
    """Fill in a form and save the pages exactly as they would be faxed, without sending anything."""
    api = state.api()
    chosen_form, chosen = _version(api, form, version)
    body = {'values': _values(value, values_file, signature)}
    kind = 'png' if picture else 'pdf'
    response = api.post(f"/forms/versions/{segment(chosen['id'])}/render", params={'format': kind}, json=body,
                        raw=True, headers={'Accept': 'image/png' if picture else 'application/pdf'})
    default = f"{chosen_form['name']} v{chosen['number']}.{kind}".replace('/', '-')
    target = save_document(response, output, default, force)
    hashes = response.headers.get('X-Faxbot-Page-Hashes', '').split(',')
    if target is not None:
        state.out().result({'saved_to': str(target), 'pages': len(hashes), 'page_hashes': hashes},
                           lambda out: out.line(f'Saved the filled form to {target}. Nothing was sent.'))


@forms.command('send')
def forms_send(form: str = typer.Argument(..., help='Form name.'), to: str = typer.Argument(..., help='Fax number.'),
               version: int = VERSION, value: list[str] = VALUE, values_file: Path = VALUES_FILE,
               signature: list[str] = SIGNATURE,
               as_fax: bool = typer.Option(False, '--as-fax', help='Send the pages as a fax even when the number '
                                                                   'belongs to a partner.')):
    """Fill in a form and send it: to a partner as the filled-in values, to anyone else as a fax."""
    api = state.api()
    _, chosen = _version(api, form, version)
    result = api.post('/forms/send', json={'version_id': chosen['id'], 'to': to,
                                           'values': _values(value, values_file, signature),
                                           'route': 'fax' if as_fax else 'auto'})

    def human(out):
        out.line(result['status'] if result['route'] == 'direct' else
                 f"Sent as a fax (fax ID {result['fax_id']}). Check on it with: faxbot status {result['fax_id']}")
        if result.get('detail') and result['state'] != 'delivered':
            out.line(result['detail'])
        if result.get('can_fax'):
            out.line(f"To send the pages as a fax instead, run: faxbot faxes forms fax {result['id']}")
    state.out().result(result, human)


@forms.command('sent')
def forms_sent(delivery: str = typer.Argument(None, help='One ID from the list, to show the values that were sent.')):
    """List forms you sent, with what happened to each; with an ID, show one with its values."""
    api = state.api()
    if delivery is not None:
        item = api.get(f'/forms/deliveries/{segment(delivery)}')
        labels = {field['name']: field['label'] for field in item.get('fields') or []}

        def one(out):
            out.fields([('Form', f"{item['form'] or '?'} v{item['form_version'] or '?'}"),
                        ('To', item['partner'] or item['fax_number']), ('Sent', local_time(item['created_at'])),
                        ('What happened', item['status'])])
            if item.get('detail') and item['state'] != 'delivered':
                out.line(item['detail'])
            if item.get('values') is None and not item.get('can_open_values'):
                out.line("You can see that this form was sent, but opening what was filled in needs access to this fax's document.")
            for name, entry in (item.get('values') or {}).items():
                out.line(f"  {labels.get(name, name)}: {'signature picture' if isinstance(entry, dict) else text(entry)}")
        state.out().result(item, one)
        return
    items = api.get('/forms/deliveries', params={'direction': 'outbound'})['deliveries']
    state.out().result(items, lambda out: out.table(
        ['Sent', 'Form', 'To', 'Went', 'What happened', 'ID'],
        [[local_time(item['created_at']), f"{item['form'] or '?'} v{item['form_version'] or '?'}",
          item['partner'] or item['fax_number'], 'to the partner' if item['route'] == 'direct' else 'as a fax',
          item['status'], item['id']] for item in items], empty='No forms sent yet.'))


@forms.command('preview')
def forms_preview(form: str = typer.Argument(..., help='Form name.'), version: int = VERSION,
                  page: int = typer.Option(1, '--page', min=1, help='Which page.'),
                  outlines: bool = typer.Option(False, '--fields', help="Outline each field's box."),
                  output: str = typer.Option(None, '--output', '-o', help="File to write. Use '-' for standard output."),
                  force: bool = typer.Option(False, '--force', help='Replace the file if it exists.')):
    """Save a page of the blank form, as it is faxed, as a PNG picture."""
    api = state.api()
    chosen_form, chosen = _version(api, form, version)
    response = api.get(f"/forms/versions/{segment(chosen['id'])}/pages/{page}",
                       params={'fields': 'true' if outlines else None}, raw=True, headers={'Accept': 'image/png'})
    default = f"{chosen_form['name']} v{chosen['number']} page {page}.png".replace('/', '-')
    target = save_document(response, output, default, force)
    if target is not None:
        state.out().result({'saved_to': str(target)}, lambda out: out.line(f'Saved page {page} to {target}.'))


@forms.command('original')
def forms_original(form: str = typer.Argument(..., help='Form name.'), version: int = VERSION,
                   output: str = typer.Option(None, '--output', '-o', help="File to write. Use '-' for standard output."),
                   force: bool = typer.Option(False, '--force', help='Replace the file if it exists.')):
    """Download the file a form version was imported from."""
    api = state.api()
    chosen_form, chosen = _version(api, form, version)
    response = api.get(f"/forms/versions/{segment(chosen['id'])}/template", raw=True)
    kind = 'svg' if 'svg' in response.headers.get('content-type', '') else 'pdf'
    target = save_document(response, output, f"{chosen_form['name']} v{chosen['number']}.{kind}".replace('/', '-'),
                           force)
    if target is not None:
        state.out().result({'saved_to': str(target)}, lambda out: out.line(f'Saved the imported file to {target}.'))


@forms.command('fax')
def forms_fax(delivery: str = typer.Argument(..., help="The ID from 'faxbot faxes forms sent'.")):
    """Send the pages of a form that did not reach the partner as an ordinary fax. Faxbot never does this by itself."""
    result = state.api().post(f'/forms/deliveries/{segment(delivery)}/fax')
    state.out().result(result, lambda out: out.line(
        result['message'] + (f" Fax ID {result['fax_id']}." if result.get('fax_id') else '')))


@forms.command('received')
def forms_received():
    """List forms partners sent whose pages matched, with the values filled in."""
    items = state.api().get('/forms/received')['received']

    def human(out):
        if not items:
            out.line('No forms received from partners yet.')
        for item in items:
            labels = {field['name']: field['label'] for field in item.get('fields') or []}
            attached = '; values attached.' if item.get('can_open_values') else '.'
            out.line(f"{local_time(item['created_at'])}: {item['form'] or 'A form'} v{item['form_version'] or '?'} "
                     f"from {item['partner']}{attached}")
            if not item.get('can_open_values'):
                out.line("  Opening what was filled in needs access to this received fax's document.")
            for name, entry in (item.get('values') or {}).items():
                shown = 'signature picture' if isinstance(entry, dict) else text(entry)
                out.line(f'  {labels.get(name, name)}: {shown}')
    state.out().result(items, human)


@forms.command('partner')
def forms_partner(partner: str = typer.Argument(..., help='Partner organization, fax number or id.')):
    """Ask a partner which forms it holds. A form it lacks is fetched from you the first time you send it."""
    from .delivery import _peer
    api = state.api()
    peer = _peer(api, partner)
    result = api.get(f"/forms/partners/{segment(peer['id'])}")
    state.out().result(result, lambda out: (out.line(result['message']), out.table(
        ['Form', 'Version', 'Also here'],
        [[item['title'], item['version'], text(item['also_here'])] for item in result['forms']], empty='')))
