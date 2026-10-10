"""Savings & optimization → Capabilities: every catalogue key, once, in a known outcome, with no money.

The capabilities module (app/routing/capabilities.py) must keep up with the mechanism catalogue, so this fails when:
- a catalogue key has no capability entry, or an entry names an unknown outcome or prerequisite kind;
- an address it emits is not a page of the six-area navigation in spec #48, or does not open in the console once
  that navigation has merged;
- a command it shows does not run as shown: it must reach a command of the faxbot app (a visible path or a hidden
  older name) and parse there, with only the {number}-style values a person fills in.

The rest evaluates GET /routing/capabilities on a real installation (SQLite and PostgreSQL) and checks that every
key appears exactly once, carries the map's own facts, names something missing whenever it does not work here, and
never carries money; and that `faxbot costs capabilities` prints the shared fixture's lines
(admin_ui/src/__tests__/capabilities.json), which the console's test renders too. All numbers and names are synthetic.
"""
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import click
import pytest
import sqlalchemy as sa
from typer.main import get_command

from app.cli.commands import capabilities as capability_commands
from app.routing import capabilities, mechanisms
from api.tests.test_cli import cli, server  # noqa: F401 (fixtures)
from api.tests.test_config_env_secrets import (  # noqa: F401 (fixtures)
    ADMIN, database_url, installation)
from api.tests.test_savings_mechanisms import AGREED, DID, JUNK, TOLL_FREE_FOR, _fax, _navigation_pages, _record

API_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = API_ROOT / 'admin_ui' / 'src' / '__tests__' / 'capabilities.json'
MODULE = 'api/app/routing/capabilities.py'
# FAXBOT_WRITE_FIXTURES=1 rewrites the shared fixture from the scenario below (SQLite run) instead of checking it.
WRITE = os.environ.get('FAXBOT_WRITE_FIXTURES') == '1'
# The capabilities the fixture also prints one by one (faxbot costs capabilities show KEY).
SHOWN = ('sending_together', 'fax_friendly', 'encoded_pages', 'direct_delivery', 'separator_pages', 'advice_plans',
         'measured_coding', 'busy_hours')

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
    '{reason}': 'Synthetic junk sender',
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


def _parse(command):
    """Parse a command line as faxbot would, hidden older names included; raises click.UsageError if it would not
    run as shown. Returns the command reached and its words."""
    from app.cli.main import app
    words = command.split()
    assert words[0] == 'faxbot', command
    node, path, rest = get_command(app), ['faxbot'], words[1:]
    while rest and isinstance(node, click.Group) and rest[0] in node.commands:
        path.append(rest[0])
        node, rest = node.commands[rest[0]], rest[1:]
    unknown = [word for word in rest if word.startswith('{') and word not in FILL_IN]
    assert unknown == [], f'{command}: no synthetic value for {unknown} in FILL_IN'
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


def test_every_address_is_a_six_area_page():
    assert set(capabilities.PAGES) <= SPEC_PAGES, sorted(set(capabilities.PAGES) - SPEC_PAGES)
    assert set(capabilities.MOVED.values()) <= set(capabilities.PAGES)
    for key, what, where in _static_addresses():
        assert _page(where) in capabilities.PAGES, (
            f'{key}: its {what} address {where} has no six-area page; add its page to MOVED or PAGES in {MODULE}')


def test_every_command_runs_as_shown():
    """Each command reaches a faxbot command (a visible path or a hidden older name) and parses there, required
    arguments and options included; the catalogue's own commands, which the read passes on, reach one too."""
    commands = [(key, capability.command) for key, capability in capabilities.CAPABILITIES.items()
                if capability.command]
    commands += [(mechanism.key, mechanism.command) for mechanism in mechanisms.CATALOGUE if mechanism.command]
    commands.append(('savings parts', 'faxbot costs savings'))
    for key, command in commands:
        assert '...' not in command, f'{key}: {command} has a value no one can run'
        try:
            _parse(command)
        except click.UsageError as error:
            raise AssertionError(f'{key}: {command!r} does not run as shown: {error.format_message()}') from None


def test_every_settings_command_changes_a_real_setting(cli):
    """The settings commands run against a real server and are accepted."""
    for key, capability in capabilities.CAPABILITIES.items():
        if capability.command and capability.command.startswith('faxbot system settings set '):
            result = cli(*capability.command.split()[1:])
            assert result.exit_code == 0, (key, result.stdout, result.stderr)


def test_every_address_opens_in_the_console():
    """Once spec #48's navigation has merged (S1, #49), every address the module emits opens a console page."""
    pages = _navigation_pages()
    if 'savings/capabilities' not in pages:
        pytest.skip('The six-area navigation (S1, #49) has not merged yet: navigation.tsx has no savings/capabilities')
    unresolved = sorted({(key, what, where) for key, what, where in _static_addresses() if _page(where) not in pages})
    assert unresolved == [], f'Addresses the console does not open: {unresolved}'


# -- a real installation -------------------------------------------------------------------------------------------

def scenario(installation):
    """A real installation with a Telnyx trunk, a recipient who shares calls, a blocked sender with one call turned
    away, a toll-free approval, faxes sent by their cheapest route and calls over fax over IP:
    (capabilities, map) as the API answers them."""
    from app.inbound.screening import Rejection, ScreeningStore
    from app.routing.tollfree import TollFreeApprovals
    settings = dict(FAX_BACKEND='sip', SIP_TRUNK_PRESET='telnyx', SIP_TRUNK_DIDS=DID, SIP_TRUNK_CALLER_ID=DID,
                    INBOUND_ENABLED='true', FAX_FRIENDLY_DOCUMENTS='never')
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
        assert (item['enabled'], item['works'], item['evidence'], item['here']) == (
            fact['enabled'], fact['works'], fact['evidence'], fact['here']), key
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
                  'costs capabilities, cli/commands/capabilities.py) show exactly what the server says. The response '
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
    assert together['results'] == {'address': 'savings/results?part=sending_together',
                                   'label': 'Savings & optimization → Savings', 'command': 'faxbot costs savings'}

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
    assert items['fax_over_ip']['setting']['command'] == 'faxbot providers trunk mode t38'
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
                                'command': 'faxbot costs recommendations plans'}
    assert items['charge_checks']['setting']['address'] == 'savings/charges'
    assert items['busy_hours']['results'] is None

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
                                                   'label': 'Delivery setup → Sender identity', 'command': None}
    header = marks['prerequisites'][-1]
    assert (header['kind'], header['met'], header['address']) == ('setting', False, 'delivery/identity')
    assert marks['improvement'] == {'kind': 'now', 'label': 'You can do this now'}

    t38 = items['fax_over_ip']
    assert (t38['enabled']['on'], t38['works']['here'], t38['ready']) == (False, False, False)
    assert [(p['kind'], p['state']) for p in t38['prerequisites']] == [
        ('connection', 'in_place'), ('connection', 'missing')]
    assert t38['filters'] == ['off', 'needs']


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
    result = cli('costs', 'capabilities')
    assert result.exit_code == 0, result.stdout
    lines = result.stdout.splitlines()
    assert lines[0] == 'Capabilities' and lines[-1] == 'One capability in full: faxbot costs capabilities show KEY'
    for _, title, _ in capabilities.OUTCOMES:
        assert title in lines
    assert '  Sending together (sending_together): Off · Not here · Test lab' in lines
    together = lines.index('  Sending together (sending_together): Off · Not here · Test lab')
    assert '    Missing: Connection, Prices, Recipient\'s agreement' in lines[together:together + 6]
    assert '$' not in result.stdout

    ready = cli('costs', 'capabilities', '--filter', 'ready')
    assert ready.exit_code == 0 and 'Showing: Ready to turn on' in ready.stdout.splitlines()
    assert all(item['ready'] for outcome in cli.json('costs', 'capabilities', '--filter', 'ready')['outcomes']
               for item in outcome['capabilities'])
    refused = cli('costs', 'capabilities', '--filter', 'cheap')
    assert refused.exit_code != 0 and 'Use one of on, off, ready, needs, experimental for --filter.' in (
        refused.stdout + (refused.stderr or ''))

    shown = cli('costs', 'capabilities', 'show', 'sslfax')
    assert shown.exit_code == 0, shown.stdout
    lines = shown.stdout.splitlines()
    assert lines[:2] == ['Faster pages', 'On · Not here · Test lab']
    assert 'Command line: faxbot system settings set sip_sslfax_enabled=true' in lines
    assert 'Its figures: Savings & optimization → Savings (faxbot costs savings)' in lines
    assert cli.json('costs', 'capabilities', 'show', 'sslfax')['key'] == 'sslfax'
    unknown = cli('costs', 'capabilities', 'show', 'cheapest')
    assert unknown.exit_code != 0
    assert 'There is no capability cheapest. List them with: faxbot costs capabilities' in (
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
