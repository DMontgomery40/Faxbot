"""faxbot admin setup: suggested packs of rules and settings, from what Faxbot already knows.

The console's Administration → Setup, Suggested packs. ``plan`` previews a plan (it
changes nothing), ``show`` shows one, and ``apply`` applies its chosen
suggestions in one step. Previewing and applying need settings:write.
"""
import typer

from .. import resolve, state
from ..errors import CliError
from ..output import local_time

setup = typer.Typer(help='Suggested packs of rules and settings from what Faxbot already knows: preview a plan, '
                         'see it, and apply it in one step.', no_args_is_help=True)

KINDS = {'rule': 'Sending rule', 'setting': 'Setting', 'step': 'You do this', 'in_effect': 'Already on'}
OWNERS = {'you': 'Your choice', 'faxbot': 'Not in Faxbot yet'}


def _money(saving):
    if not saving:
        return '-'
    from ..output import money_amount
    return f"About {money_amount(saving)} a month"


def _numbered(plan):
    """Every suggestion with the number people use to choose it, in the plan's order."""
    return [(index, item) for index, item in enumerate(
        (item for pack in plan['packs'] for item in pack['items']), start=1)]


def _human(plan):
    def show(out):
        who = f" by {plan['actor_name']}" if plan.get('actor_name') else ''
        out.line(f"Setup plan {plan['number']}, made {local_time(plan['created_at'])}{who}.")
        numbered = dict((item['key'], index) for index, item in _numbered(plan))
        for pack in plan['packs']:
            if not pack['items']:
                continue
            saving = f" Chosen suggestions save {_money(pack['saving']).lower()}." if pack.get('saving') else ''
            out.line('')
            out.line(f"{pack['title']}: {pack['sentence']}{saving}")
            out.table(['#', 'Kind', 'Suggestion', 'Saving', 'Chosen'],
                      [[numbered[item['key']], KINDS[item['kind']], item['title'], _money(item.get('saving')),
                        'Yes' if item['selected'] else ('Blocked' if item.get('blocked') else 'No')]
                       for item in pack['items']])
            for item in pack['items']:
                out.line(f"  {numbered[item['key']]}. {item['sentence']}")
                if item.get('blocked'):
                    out.line(f"     {item['blocked']}")
                if item['kind'] == 'step' and item.get('cli'):
                    out.line(f"     Do it with: {item['cli']}")
        if plan['missing']:
            out.line('')
            out.table(['Who', 'What is missing', 'Affects', 'Status'],
                      [[OWNERS[entry['owner']], entry['sentence'], entry['operation'],
                        'Holds it back' if entry['status'] == 'blocking' else 'Warning'] for entry in plan['missing']],
                      title="What's missing")
        if plan['mailboxes']:
            out.line('')
            out.line('Each mailbox:')
            for view in plan['mailboxes']:
                out.table(['Setting', 'Value', 'From'],
                          [[choice['label'], choice['value'], choice['source']] for choice in view['choices']],
                          title=view['name'])
        for application in plan.get('applications') or ():
            out.line(f"Applied {local_time(application['created_at'])}: {len(application['items'])} "
                     f"{'suggestion' if len(application['items']) == 1 else 'suggestions'} ({application['outcome']}).")
        if any(item['selected'] for _, item in _numbered(plan)):
            out.line('')
            out.line(f"Apply the chosen suggestions with: faxbot admin setup apply {plan['number']}")
    return show


def _countries(api, values):
    found = {}
    for value in values or ():
        name, _, country = value.rpartition('=')
        if not name or not country:
            raise CliError(f"Give a mailbox and its country as NAME=COUNTRY, such as 'Front desk=US', not '{value}'.")
        found[resolve.mailbox(api, name)['id']] = {'country': country.strip().upper()}
    return found


@setup.command('plan')
def setup_plan(name: str = typer.Option('', '--name', help='Your business name, printed at the top of each page.'),
               country: str = typer.Option('', '--country', help='The country your organization works in, such as '
                                                                  'US or GB. Leave out if you are not sure.'),
               mailbox_country: list[str] = typer.Option(None, '--mailbox-country',
                                                         help="A mailbox and the country it works in, as "
                                                              "'NAME=COUNTRY'. Repeat for each mailbox.")):
    """Preview a plan from what Faxbot already knows. Changes nothing and sends nothing."""
    api = state.api()
    body = {'organization_name': name, 'country': country, 'mailboxes': _countries(api, mailbox_country)}
    plan = api.post('/setup/plans', json=body)
    state.out().result(plan, _human(plan))


@setup.command('list')
def setup_list():
    """List saved setup plans so you can inspect an earlier plan with show."""
    result = state.api().get('/setup/plans')

    def human(out):
        rows = result['plans']
        if not rows:
            out.line('No setup plans have been saved yet.')
            return
        out.table(['Plan', 'Created', 'Created by'],
                  [[row['number'], local_time(row['created_at']), row.get('actor_name') or 'Not recorded']
                   for row in rows])
        out.line('Open a plan with: faxbot admin setup show NUMBER')
    state.out().result(result, human)


@setup.command('show')
def setup_show(number: int = typer.Argument(None, help='The plan number. Default: the newest plan.')):
    """Show a plan: its suggestions, what's missing and each mailbox's settings."""
    api = state.api()
    if number is None:
        plan = api.get('/setup/plans/latest')['plan']
        if plan is None:
            raise CliError("There is no setup plan yet. Preview one with 'faxbot admin setup plan'.")
    else:
        plan = api.get(f'/setup/plans/{number}')
    state.out().result(plan, _human(plan))


@setup.command('apply')
def setup_apply(number: int = typer.Argument(..., help='The plan number, from faxbot admin setup plan.'),
                only: str = typer.Option('', '--only', help="The suggestions to apply, by number, such as '1,4'. "
                                                            'Default: every suggestion the plan chose.')):
    """Apply a plan's chosen suggestions in one step; refused if your settings or rules changed since."""
    api = state.api()
    plan = api.get(f'/setup/plans/{number}')
    items = None
    if only.strip():
        numbered = dict(_numbered(plan))
        try:
            picked = [int(part) for part in only.replace(' ', '').split(',') if part]
        except ValueError:
            raise CliError("Give the suggestions by number, separated by commas, such as '1,4'.") from None
        if any(index not in numbered for index in picked):
            raise CliError(f"Plan {number} has suggestions 1 to {len(numbered)}.")
        items = [numbered[index]['key'] for index in picked]
    result = api.post(f'/setup/plans/{number}/apply', json={'expected_revision': plan['revision'], 'items': items})

    def human(out):
        out.line(result['sentence'])
        for step in result['steps']:
            out.line(f"  {'Settings' if step['part'] == 'settings' else 'Rules'}: {step['sentence']}")
    state.out().result(result, human)
