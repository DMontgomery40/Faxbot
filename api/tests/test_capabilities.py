"""Savings & optimization → Capabilities: every catalogue key, once, in a known outcome, with no money.

The capabilities module (app/routing/capabilities.py) must keep up with the mechanism catalogue, so this fails when:
- a catalogue key has no capability entry, or an entry names an unknown outcome or prerequisite kind;
- an address it emits is not a page of the six-area navigation in spec #48, or does not open in the console once
  that navigation has merged;
- a command it shows does not run as shown: it must reach a command of the faxbot app (a visible path or a hidden
  older name) and parse there, with only the {number}-style values a person fills in.

The rest evaluates GET /routing/capabilities on a real installation (SQLite and PostgreSQL) and checks that every
key appears exactly once, carries the map's own facts, names something missing whenever it does not work here, and
never carries money; and that `faxbot savings capabilities` prints the shared fixture's lines
(admin_ui/src/__tests__/capabilities.json), which the console's test renders too. All numbers and names are synthetic.
"""
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa
# Typer builds its commands with its own copy of Click (as test_cli.py's Context does), so its errors are these.
from typer._click.exceptions import UsageError
from typer.main import get_command

from app.cli.commands import capabilities as capability_commands
from app.routing import capabilities, capability_here, mechanisms
from api.tests.test_cli import cli, server  # noqa: F401 (fixtures)
from api.tests.test_config_env_secrets import (  # noqa: F401 (fixtures)
    ADMIN, database_url, installation)
from api.tests.test_savings_mechanisms import AGREED, DID, JUNK, TOLL_FREE_FOR, _fax, _navigation_pages, _record

API_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = API_ROOT / 'admin_ui' / 'src' / '__tests__' / 'capabilities.json'
MODULE = 'api/app/routing/capabilities.py'
HERE_MODULE = 'api/app/routing/capability_here.py'
# FAXBOT_WRITE_FIXTURES=1 rewrites the shared fixture from the scenario below (SQLite run) instead of checking it.
WRITE = os.environ.get('FAXBOT_WRITE_FIXTURES') == '1'
# The capabilities the fixture also prints one by one (faxbot savings capabilities show KEY).
SHOWN = ('sending_together', 'fax_friendly', 'encoded_pages', 'direct_delivery', 'separator_pages', 'advice_plans',
         'measured_coding', 'busy_hours', 'caller_id_prices', 'measured_account', 'advice_toll_free',
         'keys_after_answer')

# Spec #48's navigation table: every page of the six areas, as 'area/page'.
SPEC_PAGES = {
    'overview/overview',
    *(f'savings/{page}' for page in ('capabilities', 'opportunities', 'facts', 'results', 'spending', 'charges',
                                     'invoices', 'prices')),
    *(f'faxes/{page}' for page in ('received', 'sent', 'send', 'expected', 'forms', 'cases')),
    *(f'delivery/{page}' for page in ('numbers', 'mailboxes', 'moves', 'blocked', 'identity', 'email', 'connectors',
                                      'connections', 'rules', 'trunk', 'change', 'humblefax', 'efax', 'phaxio',
                                      'sinch', 'signalwire', 'documo')),
    'recipients/list', 'recipients/partners',
    *(f'admin/{page}' for page in ('users', 'groups', 'roles', 'who', 'keys', 'sessions', 'setup', 'security',
                                   'retention', 'analysis', 'npi', 'health', 'audit', 'logs', 'api', 'assistants',
                                   'terminal', 'scripts', 'plugins')),
}

# The values a person fills in, as the commands name them, with a synthetic value each to check that they parse.
FILL_IN = {
    '{number}': AGREED, '{partner}': 'Synthetic partner', '{route}': 'sip', '{address}': '10.20.0.2',
    '{toll_free}': '+18005550100', '{who}': 'Synthetic intake lead', '{date}': '2026-10-03',
    '{evidence}': 'Synthetic letter', '{plan}': 'sip', '{count}': '100', '{fax_id}': 'a' * 32,
    '{reason}': 'Synthetic junk sender', '{keys}': '2w105', '{class}': 'premium',
    '{notice}': 'Confidential: for the addressee only.', '{mailbox}': 'Synthetic mailbox',
    # An option that must name an existing file: any existing file shows it parses.
    '{file}': str(FIXTURE),
}


def _page(where):
    return where.partition('?')[0]


def _static_addresses():
    """Every console address the module can emit, with where it comes from."""
    found = []
    for mechanism in mechanisms.CATALOGUE:
        capability = capabilities.CAPABILITIES.get(mechanism.key)
        found.append((mechanism.key, 'setting', capabilities.address(mechanism.page)))
        if mechanism.destination:
            found.append((mechanism.key, 'results', capabilities.address(mechanism.destination)))
        for prerequisite in capability.prerequisites if capability else ():
            found.append((mechanism.key, 'prerequisite', prerequisite.address))
    # Pages the catalogue's evaluation may send a setting to this time (separator pages → Sender identity).
    found.append(('separator_pages', 'setting', capabilities.address('numbers/identity')))
    return found


def _parse(command, whole=True):
    """Parse a command line as faxbot would, hidden older names included; raises UsageError if it would not run as
    shown. With whole, it must reach a command, not a group that only lists its own. Returns the command reached and
    its words."""
    from app.cli.main import app
    words = command.split()
    assert words[0] == 'faxbot', command
    node, path, rest = get_command(app), ['faxbot'], words[1:]
    while rest and rest[0] in (getattr(node, 'commands', None) or {}):
        path.append(rest[0])
        node, rest = node.commands[rest[0]], rest[1:]
    if whole:
        # It must reach a command that runs on its own, not stop at a group that would ask for one.
        assert not getattr(node, 'commands', None) or getattr(node, 'invoke_without_command', False), (
            f'{command}: stops at the group {" ".join(path)}')
    unknown = [word for word in rest if word.startswith('{') and word not in FILL_IN]
    assert unknown == [], f'{command}: no synthetic value for {unknown} in FILL_IN'
    if whole or not getattr(node, 'commands', None):
        # A group named on its own lists its commands; there is nothing more to parse.
        node.make_context(' '.join(path), [FILL_IN.get(word, word) for word in rest])
    return node, path


# -- the module keeps up with the catalogue ------------------------------------------------------------------------

def test_every_catalogue_key_has_one_capability_in_a_known_outcome():
    keys = [mechanism.key for mechanism in mechanisms.CATALOGUE]
    missing = [key for key in keys if key not in capabilities.CAPABILITIES]
    assert missing == [], (f'Catalogue keys with no capability: add one line for each to CAPABILITIES in {MODULE} '
                           f'(its outcome, an example and what it needs): {missing}')
    extra = sorted(set(capabilities.CAPABILITIES) - set(keys))
    assert extra == [], f'{MODULE} has capabilities for keys the catalogue does not have: {extra}'
    outcomes = [key for key, _, _ in capabilities.OUTCOMES]
    assert len(outcomes) == 7 == len(set(outcomes))
    for key in keys:
        capability = capabilities.CAPABILITIES[key]
        assert capability.outcome in outcomes, f'{key}: unknown outcome {capability.outcome!r} in {MODULE}'
        assert capability.example.endswith('.') and '$' not in capability.example, key
        assert isinstance(capability.experimental, bool), key
        for prerequisite in capability.prerequisites:
            assert prerequisite.kind in capabilities.KINDS, f'{key}: unknown prerequisite kind {prerequisite.kind!r}'
            assert prerequisite.sentence.endswith('.') and callable(prerequisite.met), key
        # Advice and charge checks have nothing to switch: their home is what they found.
        assert capability.findings == (mechanisms.BY_KEY[key].stage == 'advice' or key == 'charge_checks'), key
        assert not (capability.findings and capability.command), key
    # Every outcome serves something; the four the README calls experimental are marked.
    assert {capability.outcome for capability in capabilities.CAPABILITIES.values()} == set(outcomes)
    assert sorted(key for key, capability in capabilities.CAPABILITIES.items() if capability.experimental) == [
        'encoded_pages', 'partner_repair', 'partner_tunnel', 'reuse']
    # Measured coding is automatic: no command, only its page.
    assert capabilities.CAPABILITIES['measured_coding'].command is None


def test_every_catalogue_key_says_what_it_did_here():
    """Each entry's "What it did here" line comes from its Savings part, from a counter in capability_here.py, or
    from the catalogue's own sentence that Faxbot keeps no count of it; a new entry with none of those fails here."""
    keys = [mechanism.key for mechanism in mechanisms.CATALOGUE]
    # Only these two keep no record at all (each works out its wait afresh); their catalogue line says so.
    no_count = {'busy_hours', 'free_line'}
    silent = [key for key in keys if not mechanisms.BY_KEY[key].part and key not in capability_here.COUNTERS
              and key not in no_count]
    assert silent == [], (f'Catalogue keys with no "What it did here" line: add a counter for each to COUNTERS in '
                          f'{HERE_MODULE}, counted from its own stored records: {silent}')
    assert {key for key in mechanisms.NO_PART if key not in capability_here.COUNTERS} == no_count, (
        f'A catalogue line that only says where the figures are not needs a counter in {HERE_MODULE}')
    assert sorted(set(capability_here.COUNTERS) - set(keys)) == [], f'{HERE_MODULE} counts keys the catalogue lacks'
    # A counter never replaces the count of a Savings part.
    assert [key for key in capability_here.COUNTERS if mechanisms.BY_KEY[key].part] == []
    # Where the catalogue's line only says where the figures are not, this page counts what was done instead.
    assert {'answer_cap', 'station_check', 'keys_after_answer'} <= set(capability_here.COUNTERS)


def test_every_address_is_a_six_area_page():
    assert set(capabilities.PAGES) <= SPEC_PAGES, sorted(set(capabilities.PAGES) - SPEC_PAGES)
    assert set(capabilities.MOVED.values()) <= set(capabilities.PAGES)
    for key, what, where in _static_addresses():
        assert _page(where) in capabilities.PAGES, (
            f'{key}: its {what} address {where} has no six-area page; add its page to MOVED or PAGES in {MODULE}')


def _navigation_labels():
    """{'area/page': 'Area → Page'} as navigation.tsx names them."""
    text = (API_ROOT / 'admin_ui' / 'src' / 'navigation.tsx').read_text(encoding='utf-8')
    body = text[text.index('export const NAVIGATION'):]
    starts = [(match.start(), match.group(1), match.group(2)) for match in re.finditer(
        r"\{\s*id: '([a-z]+)', label: '([^']+)', icon: <\w+ />,\s*pages: \[", body)]
    labels = {}
    for index, (start, area, area_label) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else len(body)
        for page, page_label in re.findall(r"\{ id: '([a-z-]+)', label: '([^']+)'", body[start:end]):
            labels[f'{area}/{page}'] = f'{area_label} → {page_label}'
    return labels


def test_every_page_is_named_as_the_menu_names_it():
    labels = _navigation_labels()
    assert labels.get('savings/capabilities') == 'Savings & optimization → Capabilities', 'navigation.tsx was not read'
    for page, label in capabilities.PAGES.items():
        if page == 'delivery/trunk':
            continue  # named after its carrier
        expected = 'Recipients' if page == 'recipients/list' else labels.get(page)
        assert label == expected, f'{page}: {MODULE} names it {label!r}; the menu says {expected!r}'


def test_every_command_runs_as_shown():
    """Each command this module shows reaches a faxbot command (a visible path or a hidden older name) and parses
    there, required arguments and options included. The catalogue's own commands, which the read passes on as its
    figures, must reach a command or group too (two name a group that lists its findings by subcommand)."""
    commands = [(key, capability.command, True) for key, capability in capabilities.CAPABILITIES.items()
                if capability.command]
    commands += [(mechanism.key, mechanism.command, False) for mechanism in mechanisms.CATALOGUE if mechanism.command]
    commands.append(('savings parts', 'faxbot savings results', True))
    for key, command, whole in commands:
        assert '...' not in command, f'{key}: {command} has a value no one can run'
        try:
            _parse(command, whole)
        except UsageError as error:
            raise AssertionError(f'{key}: {command!r} does not run as shown: {error.format_message()}') from None


def test_every_command_shown_uses_its_visible_name_in_the_reference():
    """The commands this module shows, and its own, use the six-area names (never an older hidden one) and are
    headings in the generated command reference."""
    from app.cli.nouns import canonical
    reference = (API_ROOT.parent / 'docs' / 'reference' / 'cli.md').read_text(encoding='utf-8')
    shown = [(key, capability.command) for key, capability in capabilities.CAPABILITIES.items() if capability.command]
    shown += [('list', capability_commands.COMMAND), ('show', f'{capability_commands.COMMAND} show KEY')]
    for key, command in shown:
        _, path = _parse(command.replace(' KEY', ' sslfax'))
        words = tuple(path[1:])
        assert canonical(words) == words, f"{key}: {command} uses an older name; use faxbot {' '.join(canonical(words))}"
        assert f"### `{' '.join(path)}`" in reference, f"{key}: {' '.join(path)} is not in docs/reference/cli.md"


def test_every_settings_command_changes_a_real_setting(cli):
    """Each settings command, run against a real server from the other value, is accepted and changes the setting."""
    opposite = {'true': 'false', 'where_it_saves': 'never'}
    commands = {capability.command for capability in capabilities.CAPABILITIES.values()
                if capability.command and capability.command.startswith('faxbot admin settings set ')}
    assert len(commands) == 4, commands
    for command in sorted(commands):
        name, value = command.split()[-1].split('=')
        before = cli('admin', 'settings', 'set', f'{name}={opposite[value]}')
        assert before.exit_code == 0, (command, before.stdout, before.stderr)
        result = cli(*command.split()[1:])
        assert result.exit_code == 0, (command, result.stdout, result.stderr)
        assert 'Nothing changed' not in result.stdout, (command, result.stdout)


def test_every_address_opens_in_the_console():
    """Once spec #48's navigation has merged (S1, #49), every address the module emits opens a console page."""
    pages = _navigation_pages()
    if 'savings/capabilities' not in pages:
        pytest.skip('The six-area navigation (S1, #49) has not merged yet: navigation.tsx has no savings/capabilities')
    unresolved = sorted({(key, what, where) for key, what, where in _static_addresses() if _page(where) not in pages})
    assert unresolved == [], f'Addresses the console does not open: {unresolved}'


# -- a real installation -------------------------------------------------------------------------------------------

def scenario(installation, **extra):
    """A real installation with a Telnyx trunk, a recipient who shares calls, a blocked sender with one call turned
    away, a toll-free approval (one suggestion now), faxes sent by their cheapest route, calls over fax over IP and a
    call that pressed keys after answer: (capabilities, map) as the API answers them."""
    from app.inbound.screening import Rejection, ScreeningStore
    from app.routing.tollfree import TollFreeApprovals
    settings = dict(FAX_BACKEND='sip', SIP_TRUNK_PRESET='telnyx', SIP_TRUNK_DIDS=DID, SIP_TRUNK_CALLER_ID=DID,
                    INBOUND_ENABLED='true', FAX_FRIENDLY_DOCUMENTS='never', **extra)
    with installation.start(**settings) as client:
        engine = installation.store().engine
        agreed = client.put(f'/batching/numbers/{AGREED}', headers=ADMIN, json={'enabled': True, 'recipient_agreed': True})
        assert agreed.status_code == 200, agreed.text
        screening = ScreeningStore(engine)
        screening.add(JUNK, 'Synthetic junk sender', actor_name='Synthetic admin')
        assert screening.record_rejection(Rejection(f'{int(datetime.utcnow().timestamp())}.1', JUNK, DID, None,
                                                    datetime.utcnow() - timedelta(minutes=5)))
        TollFreeApprovals(engine).record(TOLL_FREE_FOR, action='approved', alternate_number='+18005550100',
                                         approved_by='Synthetic clinic', approved_on=datetime.utcnow(),
                                         evidence='Synthetic letter')
        jobs = [_fax(client) for _ in range(2)]
        for job in jobs:
            _record(engine, job_id=job, route='sip', reason='cheapest_delivered')
        from app.routing.after_answer import record_submission
        assert record_submission(engine, {'Digits': '2', 'AttemptID': uuid4().hex, 'JobID': jobs[0], 'Called': AGREED})
        calls = sa.Table('sip_call_records', sa.MetaData(), autoload_with=engine)
        start = datetime.utcnow() - timedelta(days=2)
        with engine.begin() as connection:
            for index, (t38, seconds) in enumerate([('yes', 36)] * 3 + [('no', 60)] * 3):
                at = start + timedelta(minutes=index)
                connection.execute(calls.insert().values(
                    id=uuid4().hex, direction='outbound', call_id=f'synthetic-{index}', trunk_preset='telnyx',
                    caller=DID, called=AGREED, started_at=at, answered_at=at, ended_at=at, disposition='answered',
                    connected_seconds=seconds, t38=t38, pages=2, fax_status='SUCCESS', fax_preference=1,
                    created_at=at, updated_at=at))
        answer = client.get('/routing/capabilities', headers=ADMIN)
        assert answer.status_code == 200, answer.text
        mapped = client.get('/routing/savings/mechanisms', headers=ADMIN)
        assert mapped.status_code == 200, mapped.text
        return answer.json(), mapped.json()


def _by_key(body):
    return {item['key']: item for outcome in body['outcomes'] for item in outcome['capabilities']}


def _check_answer(body, mapped):
    """What holds for every answer: each key once, in its outcome, with the map's facts and no money."""
    text = json.dumps(body)
    assert '$' not in text and 'USD' not in text and '"amount"' not in text and 'micros' not in text
    assert [(outcome['key'], outcome['title'], outcome['sentence']) for outcome in body['outcomes']] == list(
        capabilities.OUTCOMES)
    listed = [item['key'] for outcome in body['outcomes'] for item in outcome['capabilities']]
    assert sorted(listed) == sorted(mechanism.key for mechanism in mechanisms.CATALOGUE)
    assert len(listed) == len(set(listed))
    # Within an outcome, catalogue order, so new entries land predictably.
    order = [mechanism.key for mechanism in mechanisms.CATALOGUE]
    for outcome in body['outcomes']:
        keys = [item['key'] for item in outcome['capabilities']]
        assert keys == sorted(keys, key=order.index)
        assert all(capabilities.CAPABILITIES[key].outcome == outcome['key'] for key in keys)
    assert [entry['key'] for entry in body['filters']] == [key for key, _, _ in capabilities.FILTERS]
    the_map = {item['key']: item for stage in mapped['stages'] for item in stage['mechanisms']}
    for key, item in _by_key(body).items():
        fact, capability = the_map[key], capabilities.CAPABILITIES[key]
        assert (item['name'], item['sentence']) == (fact['name'], fact['sentence'])
        assert (item['enabled'], item['works'], item['evidence']) == (fact['enabled'], fact['works'], fact['evidence'])
        # What it did here: the map's own line, unless the map has no count for it.
        assert item['here']['sentence'], (f'{key} says nothing under "What it did here": add a counter for it to '
                                          f'COUNTERS in {HERE_MODULE}')
        if key in capability_here.COUNTERS:
            assert item['here']['sentence'] != fact['here']['sentence'], key
            # Advice and charge checks find things; only what acted on faxes counts on the Overview.
            assert item['here']['used'] == 0 or not capability.findings, key
        else:
            assert item['here'] == fact['here'], key
        assert item['ready'] == fact['turn_on'], key
        assert item['address'] == f'savings/capabilities?key={key}'
        met = [prerequisite['met'] for prerequisite in item['prerequisites']]
        assert item['missing'] == met.count(False)
        # "Not here" always names what is missing.
        assert item['works']['here'] or item['missing'], f'{key} does not work here but lists nothing missing'
        on = item['enabled']['on']
        expected = [name for name, matches in (('on', on), ('off', not on), ('ready', item['ready']),
                                               ('needs', item['missing'] > 0), ('experimental', item['experimental']))
                    if matches]
        assert item['filters'] == expected, key
        assert (item['improvement'] is None) == (not item['ready']), key
        for prerequisite in item['prerequisites']:
            assert prerequisite['label'] == capabilities.STATES[prerequisite['state']]
            assert prerequisite['met'] == (prerequisite['state'] != 'missing')
            assert prerequisite['kind_label'] == capabilities.KINDS[prerequisite['kind']]
        assert item['setting']['address'] and item['setting']['label'], key
        # A command only where it changes this setting, on the page the setting is on this time.
        if capability.findings or item['setting']['address'] != capabilities.address(mechanisms.BY_KEY[key].page):
            assert item['setting']['command'] is None, key
        else:
            assert item['setting']['command'] == capability.command, key
        if fact['link']:
            assert item['results']['address'] == capabilities.address(fact['link'])
            assert item['results']['command'] == fact['command']
        else:
            assert item['results'] is None
        assert item['affected'] is None


def _cli_lines(body):
    return {
        'list': capability_commands.list_lines(body),
        'filtered': {'ready': capability_commands.list_lines(body, 'ready')},
        'show': {key: capability_commands.show_lines(body, key) for key in SHOWN},
    }


def _write_fixture(body):
    FIXTURE.write_text(json.dumps({
        'about': ('Capabilities in words. The console (components/capabilities/) and the faxbot command (faxbot '
                  'savings capabilities, cli/commands/capabilities.py) show exactly what the server says. The response '
                  'is GET /routing/capabilities evaluated on a synthetic installation (api/tests/test_capabilities.py '
                  'scenario), and cli holds the lines the command prints from it: all of them, those ready to turn '
                  'on, and some capabilities one by one. Its catalogue and capability fields must match '
                  'app/routing/mechanisms.py and app/routing/capabilities.py. To rewrite it after either changes, run '
                  'tests/test_capabilities.py with FAXBOT_WRITE_FIXTURES=1.'),
        'response': body,
        'cli': _cli_lines(body),
        # Every console address the module can emit; the console's test opens each one (resolvesTo).
        'addresses': sorted({where for _, _, where in _static_addresses()}),
    }, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def test_the_read_lists_every_key_once_by_outcome_and_never_shows_money(installation):
    body, mapped = scenario(installation)
    _check_answer(body, mapped)
    assert (body['title'], body['days']) == ('Capabilities', 30)
    assert body['legend'] == mapped['legend']
    items = _by_key(body)

    # Sending together: on for the recipient who agreed, on a trunk that bills whole minutes.
    together = items['sending_together']
    assert together['outcome'] == 'no_repeats' and together['filters'] == ['on']
    assert [(p['kind'], p['met']) for p in together['prerequisites']] == [
        ('connection', True), ('prices', True), ('agreement', True)]
    assert together['setting'] == {'address': 'recipients/list', 'label': 'Recipients',
                                   'command': 'faxbot recipients together set {number} --recipient-agreed'}
    the_map = {item['key']: item for stage in mapped['stages'] for item in stage['mechanisms']}
    assert together['results'] == {'address': 'savings/results?part=sending_together',
                                   'label': 'Savings & optimization → Savings results',
                                   'command': the_map['sending_together']['command']}

    # Lighter shading is off by the setting and the trunk bills by time: ready, and you can do it now.
    friendly = items['fax_friendly']
    assert friendly['filters'] == ['off', 'ready']
    assert friendly['improvement'] == {'kind': 'now', 'label': 'You can do this now'}
    assert friendly['setting']['address'] == 'delivery/connections'

    # Encoded pages: experimental, works here, and waits for a recipient's agreement.
    encoded = items['encoded_pages']
    assert encoded['filters'] == ['off', 'ready', 'needs', 'experimental']
    assert encoded['improvement'] == {'kind': 'experimental', 'label': 'Experimental'}
    assert [(p['kind'], p['met']) for p in encoded['prerequisites']] == [('connection', True), ('agreement', False)]

    # Case packets: ready, but no recipient accepts a list of documents yet.
    assert items['case_packets']['improvement'] == {'kind': 'agreement', 'label': "Needs a recipient's agreement"}

    # Direct delivery needs a partner, said with who satisfies it and where.
    direct = items['direct_delivery']
    assert direct['filters'] == ['off', 'needs'] and direct['improvement'] is None
    assert direct['prerequisites'] == [{
        'kind': 'partner', 'kind_label': 'Partner', 'met': False, 'state': 'missing', 'label': 'Missing',
        'sentence': capabilities.PARTNER.sentence, 'address': 'recipients/partners',
        'address_label': 'Recipients → Partners'}]

    # One sending route: nothing to compare yet; the trunk page carries its carrier's name.
    cheapest = items['cheapest_route']
    assert cheapest['here'] == {'used': 2, 'sentence': 'Used on 2 faxes in 30 days'}
    assert [(p['kind'], p['met']) for p in cheapest['prerequisites']] == [('connection', False), ('prices', False)]
    assert items['sslfax']['setting']['label'] == 'Delivery setup → Telnyx'
    assert items['fax_over_ip']['here']['sentence'] == 'Used on 3 calls in 30 days'
    assert items['fax_over_ip']['setting']['command'] == 'faxbot delivery providers trunk mode t38'
    # A T.38 call went through, so the network carries it; the header text is not needed while no recipient chose
    # page marks.
    assert [p['state'] for p in items['fax_over_ip']['prerequisites']] == ['in_place', 'in_place']
    assert [p['state'] for p in items['separator_pages']['prerequisites']] == [
        'in_place', 'in_place', 'missing', 'not_needed']
    # Measured coding is automatic: its page, and no command.
    assert items['measured_coding']['setting'] == {'address': 'delivery/trunk', 'label': 'Delivery setup → Telnyx',
                                                   'command': None}

    # Advice lives in its section of Opportunities; its command prints what it found.
    plans = items['advice_plans']
    assert plans['setting'] == {'address': 'savings/opportunities?section=plans',
                                'label': 'Savings & optimization → Opportunities', 'command': None}
    assert plans['results'] == {'address': 'savings/opportunities?section=plans',
                                'label': 'Savings & optimization → Opportunities',
                                'command': mechanisms.BY_KEY['advice_plans'].command}
    assert items['charge_checks']['setting']['address'] == 'savings/charges'
    assert items['busy_hours']['results'] is None

    # What it did here: the toll-free number on file is one suggestion now, the call behind a phone menu pressed keys,
    # and what found nothing says so.
    assert items['advice_toll_free']['here'] == {'used': 0, 'sentence': '1 suggestion now'}
    assert items['advice_plans']['here'] == {'used': 0, 'sentence': 'No suggestions now'}
    assert items['keys_after_answer']['here'] == {'used': 1, 'sentence': 'Pressed keys on 1 call in 30 days'}
    assert items['dialing_guard']['here'] == {'used': 0, 'sentence': 'Held no faxes in 30 days'}
    assert items['charge_checks']['here']['sentence'] == 'Found no billed faxes in 30 days that Faxbot has no record of'

    if WRITE and installation.base['DATABASE_URL'].startswith('sqlite'):
        _write_fixture(body)


def test_a_new_installation_names_what_each_capability_is_missing(installation):
    with installation.start() as client:
        body = client.get('/routing/capabilities', headers=ADMIN)
        mapped = client.get('/routing/savings/mechanisms', headers=ADMIN)
    assert body.status_code == 200 and mapped.status_code == 200, body.text
    body = body.json()
    _check_answer(body, mapped.json())
    items = _by_key(body)
    # No trunk, no partner: trunk and partner capabilities say exactly that, and none is offered to turn on.
    assert [(p['kind'], p['met']) for p in items['sslfax']['prerequisites']] == [('connection', False), ('engine', False)]
    assert items['partner_tunnel']['missing'] == 3
    # No T.38 call has gone through, so the network is not said to carry it.
    assert [p['state'] for p in items['fax_over_ip']['prerequisites']] == ['missing', 'not_checked']
    assert not items['sslfax']['ready'] and not items['direct_delivery']['ready']


def test_settings_faxbot_changed_itself_or_set_elsewhere_are_said_with_their_own_page(installation):
    """Page marks waiting for the header text lead to Sender identity, with no command for that page; fax over IP
    that Faxbot switched off for the network names the network as what is missing and offers nothing to turn on."""
    from app import sip_fax_mode
    with installation.start(FAX_BACKEND='sip', SIP_TRUNK_PRESET='telnyx', SIP_TRUNK_DIDS=DID,
                            SIP_T38_ENABLED='false') as client:
        agreed = client.put(f'/batching/numbers/{AGREED}', headers=ADMIN, json={
            'enabled': True, 'recipient_agreed': True, 'boundaries': 'page_headers', 'boundaries_agreed': True})
        assert agreed.status_code == 200, agreed.text
        sip_fax_mode.write(SimpleNamespace(fax_data_dir=installation.base['FAX_DATA_DIR']), 'audio',
                           sip_fax_mode.NETWORK)
        body = client.get('/routing/capabilities', headers=ADMIN)
        mapped = client.get('/routing/savings/mechanisms', headers=ADMIN)
    assert body.status_code == 200, body.text
    body = body.json()
    _check_answer(body, mapped.json())
    items = _by_key(body)

    marks = items['separator_pages']
    assert marks['ready'] and marks['setting'] == {'address': 'delivery/identity',
                                                   'label': 'Delivery setup → Sending identity', 'command': None}
    header = marks['prerequisites'][-1]
    assert (header['kind'], header['met'], header['address']) == ('setting', False, 'delivery/identity')
    assert marks['improvement'] == {'kind': 'now', 'label': 'You can do this now'}

    t38 = items['fax_over_ip']
    assert (t38['enabled']['on'], t38['works']['here'], t38['ready']) == (False, False, False)
    assert [(p['kind'], p['state']) for p in t38['prerequisites']] == [
        ('connection', 'in_place'), ('connection', 'missing')]
    assert t38['filters'] == ['off', 'needs']


def _insert(engine, table, **values):
    """One synthetic row of a migrated table: the given values, and a plain value for every other required column."""
    found = sa.Table(table, sa.MetaData(), autoload_with=engine)
    row = dict(values)
    for column in found.columns:
        if column.name in row or column.nullable or column.server_default is not None:
            continue
        try:
            kind = column.type.python_type
        except NotImplementedError:
            kind = str
        row[column.name] = (datetime.utcnow() if kind is datetime else 1 if kind is int else
                            uuid4().hex[:min(32, getattr(column.type, 'length', None) or 32)])
    with engine.begin() as connection:
        connection.execute(found.insert().values(**row))
    return row


def _stub(monkeypatch, target, value):
    """Replace a section's reader with one that answers ``value``: its count is what is checked, not its figures."""
    monkeypatch.setattr(target, lambda *args, **kwargs: value)


# One suggestion from each section's own reader, in the shape that reader answers. These readers work over months
# of priced faxes and calls; the empty and seeded scenarios above run every one of them for real.
ONE_SUGGESTION = {
    'advice_sending': ('app.routing.recommendations.sending_recommendations',
                       [{'kind': 'cheaper_route', 'suggested': {'route': 'sip'}}]),
    'advice_plans': ('app.routing.plan_check.plan_report',
                     {'plans': [{'state': 'keep'}, {'state': 'too_little_history'}]}),
    'advice_receiving': ('app.routing.receiving.receiving_report', {
        'pool': {'state': 'share'}, 'quiet_numbers': {'state': 'too_little_history'},
        'provider_numbers': {'state': 'none'}}),
    'advice_carriers': ('app.routing.carrier_compare.compare', {'carriers': [
        {'yours': False, 'difference': [{'currency': 'EUR', 'amount': '1.25'}]},
        {'yours': True, 'difference': []}, {'yours': False, 'difference': [{'currency': 'EUR', 'amount': '-2.00'}]}]}),
    'advice_billing_steps': ('app.routing.boundaries.billing_boundaries', {'numbers': [{'number': AGREED}]}),
    'advice_partners': ('app.routing.partners.partner_candidates', {'items': [{'number': AGREED}]}),
    'advice_fax_marker': ('app.routing.preference.fax_marker_report', {'state': 'compared'}),
    'advice_trunks': ('app.routing.trunk_advice.advice', {'items': [{}]}),
    'advice_numbers': ('app.routing.number_placement.placement',
                       {'numbers': [{'state': 'move'}, {'state': 'keep'}], 'accounts': []}),
    'advice_sites': ('app.routing.jurisdiction.site_advice', {'items': [{}]}),
    'advice_caller_names': ('app.telnyx_numbers.report',
                            {'numbers': [{'lookup': True}, {'lookup': False}, {'lookup': None}]}),
    'advice_line_inventory': ('app.routing.inventory.view', {'lines': [
        {'dates': [{'state': 'soon'}], 'match': None}, {'dates': [], 'match': {'state': 'listed'}}]}),
    'advice_copper_closures': ('app.routing.closures.view',
                               {'sites': [], 'lines': [{'state': 'soon'}, {'state': 'later'}]}),
    'advice_country_rules': ('app.routing.country_rules.view',
                             {'accounts': [{'confirmed': False}, {'confirmed': True}]}),
    'advice_renewal': ('app.routing.renewal.view', {'pages': [{}], 'channels': {'systems': [{'own': True}]}}),
    'advice_pots': ('app.routing.pots_quote.view', {'quotes': [{}]}),
}

# What each counter says about one record of its kind; advice says '1 suggestion now'.
ONE = {
    'charge_checks': 'Found 1 billed fax in 30 days that Faxbot has no record of',
    'advice_shading': ('While Lighter shading is set to Never, Faxbot measures your recent faxes for this when you '
                       'open Opportunities'),
    'measured_account': 'Used on 1 fax in 30 days',
    'dialing_guard': 'Held 1 fax for your approval in 30 days',
    'power_aware': 'Held 1 fax or sent it another way during power cuts in 30 days',
    'header_notice': 'Used on 1 fax in 30 days',
    'route_problems': 'Found 1 route problem in 30 days',
    'answer_cap': 'Hung up 1 unanswered call in 30 days',
    'station_check': 'Caught 1 call answered by a different number in 30 days',
    'keys_after_answer': 'Pressed keys on 1 call in 30 days',
}


def _power_hold_sentence():
    """The reason power-aware sending gives a fax it holds, from power.admission itself."""
    from app import power
    # A UPS reading with the two fields admission reads: on battery, two minutes left.
    on_battery = power.PowerState(True, reading=SimpleNamespace(on_battery=True, runtime_seconds=120))
    held = power.admission(on_battery, provider='sip', p90_seconds=900)
    assert held.action == 'hold' and held.sentence.startswith(capability_here.POWER_HOLD), (
        f'power.py now words its hold differently; update POWER_HOLD in {HERE_MODULE}: {held.sentence}')
    return held.sentence


def test_each_counter_counts_one_record_of_its_kind(installation, monkeypatch):
    """One record of each kind, read through GET /routing/capabilities: its count is 1 and its sentence says so.
    Advice counts suggestions and charge checks count charges; neither acts on faxes, so the Overview's "What Faxbot
    is doing" lists neither."""
    from app.cli.commands.overview import used_capabilities
    from app.direct.relay import RelayService
    from app.routing import sender_pins, stations
    from app.routing.after_answer import record_submission
    from app.routing.tollfree import TollFreeApprovals
    from app.schema_discovery import SUGGESTION_SOURCES
    from app.schema_route_families import CAUSES, PHASES, TRANSPORTS
    from app.schema_route_selections import LAYOUTS, RENDERINGS
    for target, value in ONE_SUGGESTION.values():
        _stub(monkeypatch, target, value)
    # Relay advice names only verified partners, and registered senders are checked one by one.
    monkeypatch.setattr(mechanisms.Installation, 'partners', property(lambda self: [False]))
    monkeypatch.setattr(RelayService, 'recommendations', lambda self, **kwargs: [{'partner': 'Synthetic partner'}])
    monkeypatch.setattr(sender_pins, 'pins', lambda engine: [SimpleNamespace(account='sip', caller_id=DID,
                                                                             station_id=None)])
    monkeypatch.setattr(sender_pins, 'presentable', lambda *args, **kwargs: (False, 'Synthetic reason.'))
    _stub(monkeypatch, 'app.setup_plan.store.PlanStore.latest', {'plan': {'packs': [{'items': [{}]}, {'items': []}]}})
    with installation.start(FAX_BACKEND='sip', SIP_TRUNK_PRESET='telnyx', SIP_TRUNK_DIDS=DID) as client:
        engine = installation.store().engine
        job, power_job, attempt, now = _fax(client), _fax(client), uuid4().hex, datetime.utcnow()
        TollFreeApprovals(engine).record(TOLL_FREE_FOR, action='approved', alternate_number='+18005550100',
                                         approved_by='Synthetic clinic', approved_on=now, evidence='Synthetic letter')
        _insert(engine, 'direct_discovery_suggestions', number=AGREED, source=SUGGESTION_SOURCES[0])
        _insert(engine, 'provider_unrecorded_faxes', direction='sent', provider_time=now, version=1, pages=1,
                provider_id='humblefax')
        _insert(engine, 'fax_route_selections', job_id=job, attempt_id=attempt, layout=LAYOUTS[0],
                rendering=RENDERINGS[0], measured=1, compared=2, original_pages=1, sent_pages=1, created_at=now)
        hold = _insert(engine, 'outbound_holds', job_id=job, kind='approval', state='open', separate_approver=0,
                       version=1, requested_at=now)
        _insert(engine, 'dialing_guard_holds', id=hold['id'], job_id=job, why='not_allowed', created_at=now)
        _insert(engine, 'outbound_holds', job_id=power_job, kind='no_route', state='open', separate_approver=0,
                version=1, requested_at=now, reason=_power_hold_sentence())
        _insert(engine, 'fax_header_notices', id=job, scope='organization', cover='dropped', original_pages=2,
                sent_pages=1, created_at=now)
        _insert(engine, 'route_family_incidents', transport=TRANSPORTS[0], phase=PHASES[0], change_cause=CAUSES[0],
                destinations=1, failures=1, calls=1, corroborated=0, opened_at=now)
        with engine.begin() as connection:
            stations.record_cap_on(connection, attempt_id=attempt, job_id=job, card=None, now=now)
            stations.record_on(connection, attempt_id=attempt, job_id=job, number=AGREED, station='+12025550999',
                               outcome='differs', engine='builtin', now=now)
        assert record_submission(engine, {'Digits': '2', 'AttemptID': attempt, 'JobID': job, 'Called': AGREED})
        answer = client.get('/routing/capabilities', headers=ADMIN)
    assert answer.status_code == 200, answer.text
    items = _by_key(answer.json())
    for key in capability_here.COUNTERS:
        if key == 'caller_id_prices':
            continue
        assert items[key]['here'] == {'used': 0 if capabilities.CAPABILITIES[key].findings else 1,
                                      'sentence': ONE.get(key, '1 suggestion now')}, key
    assert items['caller_id_prices']['here'] == {
        'used': 0, 'sentence': 'Faxbot keeps no count of the calls it priced by caller ID'}
    listed = {item['key'] for item in used_capabilities(answer.json())}
    assert {key for key in ONE if not capabilities.CAPABILITIES[key].findings} <= listed
    assert [key for key in listed if capabilities.CAPABILITIES[key].findings] == []


def test_the_lines_read_no_network(installation, monkeypatch):
    """Every line is read from stored records: with every way out to the network refusing, the read still answers,
    HumbleFax's account numbers included (the plans advice never asks HumbleFax for them)."""
    import urllib.request
    import httpx
    from app import humblefax_service, telnyx_t38
    from app.routing import carriers, nppes, provider_sweep

    def refuse(*args, **kwargs):
        raise AssertionError('The Capabilities read reached for the network')
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', refuse)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', refuse)
    monkeypatch.setattr(urllib.request, 'urlopen', refuse)
    for owner, name in [(humblefax_service, 'account_numbers'), (provider_sweep.ProviderSweep, 'step'),
                        (carriers.CarrierReconciler, 'sweep'), (carriers.CarrierReconciler, 'sweep_unrecorded'),
                        (telnyx_t38, 'check'), (telnyx_t38, 'check_all'),
                        (nppes, 'lookup'), (nppes, 'search'), (nppes, 'refresh_own')]:
        monkeypatch.setattr(owner, name, refuse)
    body, mapped = scenario(installation, HUMBLEFAX_API_ACCESS_KEY='synthetic-access',
                            HUMBLEFAX_API_SECRET_KEY='synthetic-secret')
    _check_answer(body, mapped)


def _refuse_findings(monkeypatch):
    """Every advice and charge-check counter raises: what is read without them never calls one."""
    def refuse(here, now):
        raise AssertionError('A findings counter ran')
    for key in capability_here.COUNTERS:
        if capabilities.CAPABILITIES[key].findings:
            monkeypatch.setitem(capability_here.COUNTERS, key, refuse)


def test_the_overview_read_never_counts_what_advice_and_charge_checks_found(installation, monkeypatch):
    """GET /routing/capabilities?findings=false, the Overview's read, leaves advice and charge checks without a line
    (the Overview never shows it) and still counts what acted on faxes; the shading advice never draws a fax."""
    from app.pages import friendly

    def refuse(*args, **kwargs):
        raise AssertionError('The shading line drew a fax')
    monkeypatch.setattr(friendly, 'recommendation', refuse)
    monkeypatch.setattr(friendly, 'measure_document', refuse)
    whole, _ = scenario(installation)
    assert _by_key(whole)['advice_shading']['here'] == {'used': 0, 'sentence': (
        'While Lighter shading is set to Never, Faxbot measures your recent faxes for this when you open '
        'Opportunities')}
    _refuse_findings(monkeypatch)
    with installation.start() as client:
        answer = client.get('/routing/capabilities', headers=ADMIN, params={'findings': 'false'})
    assert answer.status_code == 200, answer.text
    items = _by_key(answer.json())
    for key, item in items.items():
        if capabilities.CAPABILITIES[key].findings:
            assert item['here'] == {'used': 0, 'sentence': None}, key
        else:
            assert item['here']['sentence'], key
    assert items['keys_after_answer']['here'] == {'used': 1, 'sentence': 'Pressed keys on 1 call in 30 days'}


def test_faxbot_overview_never_counts_what_advice_and_charge_checks_found(cli, monkeypatch):
    """`faxbot overview` asks for the capabilities without findings, so no advice or charge-check counter runs."""
    _refuse_findings(monkeypatch)
    result = cli('overview')
    assert result.exit_code == 0, result.stdout + (result.stderr or '')
    assert 'What Faxbot is doing' in result.stdout.splitlines()


def test_the_fixture_is_an_answer_from_the_modules():
    """capabilities.json feeds the console's and the command's tests: its fixed fields are the modules' own."""
    fixture = json.loads(FIXTURE.read_text(encoding='utf-8'))
    response = fixture['response']
    regenerate = f'rewrite it: FAXBOT_WRITE_FIXTURES=1 with tests/test_capabilities.py ({FIXTURE.name})'
    assert (response['title'], response['sentence']) == (capabilities.TITLE, capabilities.SENTENCE), regenerate
    assert [(entry['label'], entry['sentence']) for entry in response['legend']] == list(mechanisms.LEGEND), regenerate
    assert [(entry['key'], entry['label'], entry['sentence']) for entry in response['filters']] == list(
        capabilities.FILTERS), regenerate
    assert [(outcome['key'], outcome['title'], outcome['sentence']) for outcome in response['outcomes']] == list(
        capabilities.OUTCOMES), regenerate
    items = _by_key(response)
    for key, item in items.items():
        mechanism, capability = mechanisms.BY_KEY[key], capabilities.CAPABILITIES[key]
        assert (item['name'], item['sentence'], item['evidence']['label']) == (
            mechanism.name, mechanism.sentence, mechanisms.EVIDENCE[mechanism.evidence]), f'{key}: {regenerate}'
        assert (item['outcome'], item['example'], item['experimental']) == (
            capability.outcome, capability.example, capability.experimental), f'{key}: {regenerate}'
        assert [(p['kind'], p['sentence'], p['address']) for p in item['prerequisites']] == [
            (p.kind, p.sentence, p.address) for p in capability.prerequisites], f'{key}: {regenerate}'
        if item['setting']['command']:
            assert item['setting']['command'] == capability.command, f'{key}: {regenerate}'
    # Every state the console and the command must show is in it at least once.
    filters = {name for item in items.values() for name in item['filters']}
    assert filters == {key for key, _, _ in capabilities.FILTERS}
    kinds = {item['improvement']['kind'] for item in items.values() if item['improvement']}
    assert {'now', 'agreement', 'experimental'} <= kinds
    assert any(item['here']['used'] for item in items.values())
    assert any(not p['met'] and p['kind'] == 'agreement' for item in items.values() for p in item['prerequisites'])
    # The command prints exactly the fixture's lines from that answer.
    assert fixture['cli'] == _cli_lines(response), regenerate
    assert fixture['addresses'] == sorted({where for _, _, where in _static_addresses()}), regenerate


def test_faxbot_costs_capabilities_lists_shows_and_filters(cli):
    result = cli('savings', 'capabilities')
    assert result.exit_code == 0, result.stdout
    lines = result.stdout.splitlines()
    assert lines[0] == 'Capabilities' and lines[-1] == 'One capability in full: faxbot savings capabilities show KEY'
    for _, title, _ in capabilities.OUTCOMES:
        assert title in lines
    assert '  Sending together (sending_together): Off · Not here · Test lab' in lines
    together = lines.index('  Sending together (sending_together): Off · Not here · Test lab')
    assert '    Missing: Connection, Prices, Recipient\'s agreement' in lines[together:together + 6]
    assert '$' not in result.stdout

    ready = cli('savings', 'capabilities', '--filter', 'ready')
    assert ready.exit_code == 0 and 'Showing: Ready to turn on' in ready.stdout.splitlines()
    assert all(item['ready'] for outcome in cli.json('savings', 'capabilities', '--filter', 'ready')['outcomes']
               for item in outcome['capabilities'])
    refused = cli('savings', 'capabilities', '--filter', 'cheap')
    assert refused.exit_code != 0 and 'Use one of on, off, ready, needs, experimental for --filter.' in (
        refused.stdout + (refused.stderr or ''))

    shown = cli('savings', 'capabilities', 'show', 'sslfax')
    assert shown.exit_code == 0, shown.stdout
    lines = shown.stdout.splitlines()
    assert lines[:2] == ['Faster pages', 'On · Not here · Test lab']
    assert 'Command line: faxbot admin settings set sip_sslfax_enabled=true' in lines
    figures = {item['key']: item for stage in cli.json('savings', 'mechanisms')['stages']
               for item in stage['mechanisms']}
    assert f"Its figures: Savings & optimization → Savings results ({figures['sslfax']['command']})" in lines
    assert cli.json('savings', 'capabilities', 'show', 'sslfax')['key'] == 'sslfax'
    unknown = cli('savings', 'capabilities', 'show', 'cheapest')
    assert unknown.exit_code != 0
    assert 'There is no capability cheapest. List them with: faxbot savings capabilities' in (
        unknown.stdout + (unknown.stderr or ''))


def test_a_key_with_no_capability_line_is_still_listed_and_the_read_still_answers(installation, monkeypatch, caplog):
    """A research entry merged without its line here must not take the read (and Overview) down; the strict test
    above still fails until the line is added."""
    trimmed = {key: capability for key, capability in capabilities.CAPABILITIES.items() if key != 'relay'}
    monkeypatch.setattr(capabilities, 'CAPABILITIES', trimmed)
    with installation.start() as client:
        body = client.get('/routing/capabilities', headers=ADMIN)
    assert body.status_code == 200, body.text
    relay = _by_key(body.json())['relay']
    assert (relay['outcome'], relay['example'], relay['prerequisites'], relay['setting']['command']) == (
        'explain', '', [], None)
    assert relay['setting']['label'] == 'Recipients → Partners'
    assert 'Capability relay has no entry in routing/capabilities.py' in caplog.text


def test_capabilities_need_settings_read(installation):
    with installation.start(FAX_BACKEND='sip') as client:
        made = client.post('/admin/api-keys', headers=ADMIN, json={'name': 'synthetic', 'scopes': ['fax:send']})
        assert made.status_code == 200, made.text
        refused = client.get('/routing/capabilities', headers={'X-API-Key': made.json()['token']})
        assert refused.status_code == 403
