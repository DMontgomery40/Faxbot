"""faxbot savings capabilities: everything Faxbot can do, grouped by what it helps with (GET /routing/capabilities).

The console's Capabilities page shows the same facts in the same sentences; admin_ui/src/__tests__/capabilities.json
holds one answer and the lines printed from it, and both surfaces' tests read it. Never any money.
"""
import typer

from .. import state
from ..errors import CliError

# The command as people type it; its hint and the shared fixture's lines use this one name.
COMMAND = 'faxbot savings capabilities'
FILTERS = ('on', 'off', 'ready', 'needs', 'experimental')

capabilities = typer.Typer(
    help='Everything Faxbot can do to make your faxes cost less and take less time, grouped by what it helps with: '
         'whether each is on, works here and is proven, and what it still needs. Run it alone to list them.',
    invoke_without_command=True)


def states(item):
    """'Off · Works here · Sample data · Ready to turn on · Experimental': the console shows the same as chips."""
    parts = [item['enabled']['label'], item['works']['label'], item['evidence']['label']]
    if item['ready']:
        parts.append('Ready to turn on')
    if item['experimental']:
        parts.append('Experimental')
    return ' · '.join(parts)


def missing_line(item):
    """'Missing: Partner, Setting' for what a capability still needs here, or None."""
    kinds = [prerequisite['kind_label'] for prerequisite in item['prerequisites'] if not prerequisite['met']]
    return f"Missing: {', '.join(kinds)}" if kinds else None


def _filter_label(result, show):
    return next(entry['label'] for entry in result['filters'] if entry['key'] == show)


def list_lines(result, show=None):
    """Every capability by outcome, or those matching one filter, in the server's own sentences."""
    lines = [result['title'], result['sentence']]
    if show:
        lines.append(f'Showing: {_filter_label(result, show)}')
    shown = 0
    for outcome in result['outcomes']:
        items = [item for item in outcome['capabilities'] if not show or show in item['filters']]
        if not items:
            continue
        lines += ['', outcome['title'], outcome['sentence']]
        for item in items:
            shown += 1
            lines.append(f"  {item['name']} ({item['key']}): {states(item)}")
            lines.append(f"    {item['sentence']}")
            for sentence in (item['enabled']['sentence'], item['works']['sentence'], missing_line(item),
                             item['here']['sentence']):
                if sentence:
                    lines.append(f'    {sentence}')
            if item['ready']:
                lines.append(f"    Turn it on in {item['setting']['label']}.")
    if not shown:
        lines += ['', f'Nothing matches {_filter_label(result, show)}.']
    lines.append('')
    lines += [f"{entry['label']}: {entry['sentence']}" for entry in result['legend']]
    lines += [f"{entry['label']}: {entry['sentence']}" for entry in result['filters'] if entry['key'] in
              ('ready', 'needs', 'experimental')]
    lines.append(f'One capability in full: {COMMAND} show KEY')
    return lines


def find(result, key):
    for outcome in result['outcomes']:
        for item in outcome['capabilities']:
            if item['key'] == key:
                return outcome, item
    return None, None


def show_lines(result, key):
    """One capability: what it does, an example, its state here, what it needs, its setting and its figures."""
    outcome, item = find(result, key)
    lines = [item['name'], states(item), item['sentence']]
    if item['example']:
        lines.append(f"For example: {item['example']}")
    lines += [sentence for sentence in (item['enabled']['sentence'], item['works']['sentence']) if sentence]
    lines.append(f"Helps with: {outcome['title']}")
    lines += ['', 'What it needs']
    for prerequisite in item['prerequisites']:
        lines.append(f"  {prerequisite['label']} · {prerequisite['kind_label']}: {prerequisite['sentence']} "
                     f"See {prerequisite['address_label']}.")
    if not item['prerequisites']:
        lines.append('  Nothing more than any installation has.')
    lines.append('')
    # Advice and charge checks keep their home on the page with what they found: their figures line says it.
    if (not item['results'] or item['setting']['address'] != item['results']['address'] or item['setting']['command']
            or item['ready']):
        lines.append(f"Its setting: {item['setting']['label']}")
    if item['setting']['command']:
        lines.append(f"Command line: {item['setting']['command']}")
    if item['ready']:
        lines.append(f"Turn it on in {item['setting']['label']}.")
    if item['here']['sentence']:
        lines.append(f"On this installation: {item['here']['sentence']}")
    if item['results']:
        lines.append(f"Its figures: {item['results']['label']} ({item['results']['command']})")
    if item['affected']:
        lines.append(f"The faxes it acted on: {item['affected']['label']}")
    return lines


@capabilities.callback()
def capabilities_list(context: typer.Context,
                      show: str = typer.Option(None, '--filter', metavar='|'.join(FILTERS),
                                               help='Only the capabilities that are on, off, ready to turn on, need '
                                                    'something, or are experimental.')):
    """List every capability, grouped by what it helps with."""
    if context.invoked_subcommand is not None:
        return
    if show is not None and show not in FILTERS:
        raise CliError(f"Use one of {', '.join(FILTERS)} for --filter.")
    result = state.api().get('/routing/capabilities')
    if show:
        result = {**result, 'outcomes': [
            {**outcome, 'capabilities': [item for item in outcome['capabilities'] if show in item['filters']]}
            for outcome in result['outcomes']]}
    state.out().result(result, lambda out: [out.line(line) for line in list_lines(result, show)])


@capabilities.command('show')
def capabilities_show(key: str = typer.Argument(..., metavar='KEY',
                                                help=f'The capability, as listed in brackets by {COMMAND}.')):
    """Show one capability: what it does, an example, whether it works here, what it needs and where to change it."""
    result = state.api().get('/routing/capabilities')
    _, item = find(result, key)
    if item is None:
        raise CliError(f'There is no capability {key}. List them with: {COMMAND}')
    state.out().result(item, lambda out: [out.line(line) for line in show_lines(result, key)])
