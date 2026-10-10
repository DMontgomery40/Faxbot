"""Savings & optimization → Capabilities: every catalogue key, once, in a known outcome, with no money.

The capabilities module (app/routing/capabilities.py) must keep up with the mechanism catalogue, so this fails when:
- a catalogue key has no capability entry, or an entry names an unknown outcome or prerequisite kind;
- an address it emits is not a page of the six-area navigation in spec #48, or does not open in the console once
  that navigation has merged;
- a command it names is not in the command reference.

The rest evaluates GET /routing/capabilities on a real installation (SQLite and PostgreSQL) and checks that every
key appears exactly once, carries the map's own facts, names something missing whenever it does not work here, and
never carries money. The shared fixture (admin_ui/src/__tests__/capabilities.json) is one such answer; the console's
and the command's tests read it. All numbers and names are synthetic.
"""
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa

from app.routing import capabilities, mechanisms
from api.tests.test_config_env_secrets import (  # noqa: F401 (fixtures)
    ADMIN, database_url, installation)
from api.tests.test_savings_mechanisms import AGREED, DID, JUNK, TOLL_FREE_FOR, _fax, _navigation_pages, _record

API_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = API_ROOT / 'admin_ui' / 'src' / '__tests__' / 'capabilities.json'
REFERENCE = API_ROOT.parent / 'docs' / 'reference' / 'cli.md'
MODULE = 'api/app/routing/capabilities.py'
# FAXBOT_WRITE_FIXTURES=1 rewrites the shared fixture from the scenario below (SQLite run) instead of checking it.
WRITE = os.environ.get('FAXBOT_WRITE_FIXTURES') == '1'

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


def _page(where):
    return where.partition('?')[0]


def _heading(command):
    """The command a line names, without its arguments: '{number}', '--option' and 'name=value' end it."""
    words = []
    for word in command.split():
        if word.startswith(('{', '-')) or '=' in word:
            break
        words.append(word)
    return ' '.join(words)


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
        for page, _ in capability.elsewhere if capability else ():
            found.append((mechanism.key, 'setting elsewhere', page))
    # Pages the catalogue's evaluation may send a setting to this time (separator pages → Sender identity).
    found.append(('separator_pages', 'setting', capabilities.address('numbers/identity')))
    return found


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
        # Advice and charge checks keep the catalogue's own command, the one that prints what they found.
        assert (capability.command is None) == (mechanisms.BY_KEY[key].stage == 'advice' or key == 'charge_checks'), key
    # Every outcome serves something.
    assert {capability.outcome for capability in capabilities.CAPABILITIES.values()} == set(outcomes)
    assert capabilities.CAPABILITIES['encoded_pages'].experimental


def test_every_address_is_a_six_area_page_and_every_command_is_in_the_reference():
    assert set(capabilities.PAGES) <= SPEC_PAGES, sorted(set(capabilities.PAGES) - SPEC_PAGES)
    assert set(capabilities.MOVED.values()) <= set(capabilities.PAGES)
    for key, what, where in _static_addresses():
        assert _page(where) in capabilities.PAGES, (
            f'{key}: its {what} address {where} has no six-area page; add its page to MOVED or PAGES in {MODULE}')
    reference = REFERENCE.read_text(encoding='utf-8')
    for key, capability in capabilities.CAPABILITIES.items():
        for command in [capability.command, *(command for _, command in capability.elsewhere)]:
            if command is None:
                continue
            assert command.startswith('faxbot ') and f'### `{_heading(command)}`' in reference, (
                f'{key}: no command {_heading(command)!r} in docs/reference/cli.md')


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
        fact = the_map[key]
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
            assert prerequisite['label'] == ('In place' if prerequisite['met'] else 'Missing')
            assert prerequisite['kind_label'] == capabilities.KINDS[prerequisite['kind']]
        assert item['setting']['address'] and item['setting']['label'] and item['setting']['command'], key
        if fact['link']:
            assert item['results']['address'] == capabilities.address(fact['link'])
            assert item['results']['command'] == fact['command']
        else:
            assert item['results'] is None
        assert item['affected'] is None


def _write_fixture(body):
    FIXTURE.write_text(json.dumps({
        'about': ('Capabilities in words. The console (components/capabilities/) and the faxbot command (faxbot '
                  'costs capabilities) show exactly what the server says. The response is GET /routing/capabilities '
                  'evaluated on a synthetic installation (api/tests/test_capabilities.py scenario); its catalogue and '
                  'capability fields must match app/routing/mechanisms.py and app/routing/capabilities.py. To rewrite '
                  'it after either changes, run tests/test_capabilities.py with FAXBOT_WRITE_FIXTURES=1.'),
        'response': body,
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
    assert together['setting'] == {'address': 'recipients/list', 'label': 'Recipients → Details',
                                   'command': 'faxbot recipients together set {number}'}
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
        'kind': 'partner', 'kind_label': 'Partner', 'met': False, 'label': 'Missing',
        'sentence': capabilities.PARTNER.sentence, 'address': 'recipients/partners',
        'address_label': 'Recipients → Partners'}]

    # One sending route: nothing to compare yet; the trunk page carries its carrier's name.
    cheapest = items['cheapest_route']
    assert cheapest['here'] == {'used': 2, 'sentence': 'Used on 2 faxes in 30 days'}
    assert [(p['kind'], p['met']) for p in cheapest['prerequisites']] == [('connection', False), ('prices', False)]
    assert items['sslfax']['setting']['label'] == 'Delivery setup → Telnyx'
    assert items['fax_over_ip']['here']['sentence'] == 'Used on 3 calls in 30 days'

    # Advice lives in its section of Opportunities and keeps the catalogue's command.
    plans = items['advice_plans']
    assert plans['setting'] == plans['results'] == {
        'address': 'savings/opportunities?section=plans', 'label': 'Savings & optimization → Opportunities',
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
    # No trunk, no partner: trunk and partner capabilities say exactly that.
    assert [(p['kind'], p['met']) for p in items['sslfax']['prerequisites']] == [('connection', False), ('engine', False)]
    assert items['partner_tunnel']['missing'] == 3
    # Nothing is ready to turn on that needs a trunk or a partner.
    assert not items['sslfax']['ready'] and not items['direct_delivery']['ready']


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
    # Every state the console and the command must show is in it at least once.
    filters = {name for item in items.values() for name in item['filters']}
    assert filters == {key for key, _, _ in capabilities.FILTERS}
    kinds = {item['improvement']['kind'] for item in items.values() if item['improvement']}
    assert {'now', 'agreement', 'experimental'} <= kinds
    assert any(item['here']['used'] for item in items.values())
    assert any(not p['met'] and p['kind'] == 'agreement' for item in items.values() for p in item['prerequisites'])


def test_capabilities_need_settings_read(installation):
    with installation.start(FAX_BACKEND='sip') as client:
        made = client.post('/admin/api-keys', headers=ADMIN, json={'name': 'synthetic', 'scopes': ['fax:send']})
        assert made.status_code == 200, made.text
        refused = client.get('/routing/capabilities', headers={'X-API-Key': made.json()['token']})
        assert refused.status_code == 403
