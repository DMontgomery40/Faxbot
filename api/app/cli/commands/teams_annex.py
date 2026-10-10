"""faxbot providers trunk teams-annex: Faxbot as the fax annex behind a Microsoft Teams Direct Routing SBC (N21)."""
import typer

from .. import state
from ..errors import CliError

VENDORS = {'audiocodes': 'teams-sbc-audiocodes', 'ribbon': 'teams-sbc-ribbon', 'oracle': 'teams-sbc-oracle',
           'anynode': 'teams-sbc-anynode'}


def teams_annex(vendor: str = typer.Option(..., '--vendor', metavar='audiocodes|ribbon|oracle|anynode',
                                           help='The company that makes your Teams Direct Routing SBC.'),
                address: str = typer.Option(None, '--address', metavar='ADDRESS',
                                            help="Faxbot's address on your network, written into the steps; "
                                                 'Faxbot fills it in when it is published there.'),
                numbers: str = typer.Option(None, '--numbers', metavar='NUMBERS',
                                            help='Your fax numbers or their pattern, written into the steps.')):
    """Print what to set on your Teams Direct Routing SBC so the fax numbers reach Faxbot before Teams, and the checklist before the Teams port order."""
    preset_id = VENDORS.get(vendor.strip().lower())
    if preset_id is None:
        raise CliError('Use audiocodes, ribbon, oracle or anynode for --vendor.')
    api = state.api()
    catalog = {item['id']: item for item in api.get('/admin/sip/presets').get('presets') or []}
    chosen = catalog.get(preset_id)
    if chosen is None:
        raise CliError('This Faxbot does not know the Teams SBC presets yet. Update Faxbot and try again.')
    if address is None:
        try:
            reach = (api.get('/admin/sip/status') or {}).get('phone_system') or {}
            address = reach.get('address')
        except CliError:
            address = None
    steps = []
    for step in chosen['admin_steps']:
        if address:
            step = step.replace("Faxbot's address", f"Faxbot's address ({address})")
        if numbers:
            step = step.replace('your fax numbers', f'your fax numbers ({numbers.strip()})')
        steps.append(step)
    result = {'preset': preset_id, 'label': chosen['label'], 'steps': steps,
              'port_checklist': chosen.get('port_checklist') or [], 'notes': chosen['notes'],
              'sources': chosen['sources']}

    from .trunk import local_date

    def human(out):
        out.line(f"What you set in {chosen['label'].split(': ', 1)[-1]}:")
        for number, step in enumerate(steps, 1):
            out.line(f'{number}. {step}')
        out.line('')
        out.line('Before the Teams port order:')
        for number, step in enumerate(result['port_checklist'], 1):
            out.line(f'{number}. {step}')
        out.line('')
        for note in chosen['notes']:
            out.line(note)
        out.line('')
        out.line('Sources:')
        for source in chosen['sources']:
            out.line(f"- {source['url']} (read {local_date(source['read_on'])})")
        out.line('')
        out.line(f'Then choose this preset for the trunk: faxbot providers trunk use {preset_id} --host <SBC address>.')
    state.out().result(result, human)


def copiers(copier: str = typer.Argument(None, metavar='[COPIER]', help='Show one copier in full, such as ricoh-im.')):
    """List the copier makers whose documents show fax over the network with SIP and T.38, or show what to set on one."""
    items = state.api().get('/admin/sip/copiers').get('copiers') or []
    from .trunk import local_date
    if copier:
        chosen = next((item for item in items if item['id'] == copier), None)
        if chosen is None:
            raise CliError(f"No copier is called '{copier}'. Run faxbot providers trunk copiers to list them.")

        def detail(out):
            out.line(f"{chosen['label']}: {chosen['summary']}")
            for number, step in enumerate(chosen['steps'], 1):
                out.line(f'{number}. {step}')
            for source in chosen['sources']:
                out.line(f"- {source['url']} (read {local_date(source['read_on'])})")
        state.out().result(chosen, detail)
        return
    rows = [[item['id'], item['label'], 'Yes' if item['sip_t38'] else 'Not found'] for item in items]
    state.out().result({'copiers': items}, lambda out: out.table(['Copier', 'Name', 'SIP fax with T.38'], rows,
                                                                  empty='No copiers.'))
