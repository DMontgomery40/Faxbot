"""The savings map: one catalogue entry for every way Faxbot saves money, evaluated for a real installation.

The catalogue (app/routing/mechanisms.py) must stay complete, so this fails when:
- a part of GET /routing/savings has no catalogue entry;
- an entry names a setting that is not a configuration value, a console page navigation.tsx does not have, or a
  Savings part the console's Savings page does not render with its anchor;
- an entry has no product evidence level, or has no Savings part without a recorded reason.

The rest evaluates the catalogue on a real installation (SQLite and PostgreSQL) with real settings and history
rows written by the stores that write them in production, and checks that the map never carries money and that
GET /routing/savings/mechanisms follows settings:read. All numbers and names are synthetic.
"""
from datetime import datetime, timedelta
import json
from pathlib import Path
import re
from uuid import uuid4

import pytest
import sqlalchemy as sa

from app.config_values import ConfigurationValues
from app.routing import mechanisms
from api.tests.test_cli import cli, server  # noqa: F401 (fixtures)
from api.tests.test_config_env_secrets import (  # noqa: F401 (fixtures)
    ADMIN, Installation, database_url, installation)

API_ROOT = Path(__file__).resolve().parents[1]
CONSOLE = API_ROOT / 'admin_ui' / 'src'
SAVINGS_SCREEN = CONSOLE / 'components' / 'delivery' / 'Savings.tsx'
FIXTURE = CONSOLE / '__tests__' / 'savingsMap.json'
DID, AGREED, JUNK, TOLL_FREE_FOR = '+13035550100', '+12025550123', '+12025550111', '+12025550144'


def _savings_parts(body):
    """The parts of a GET /routing/savings answer: every value that is a part with its own sentence."""
    return {key for key, value in body.items() if isinstance(value, dict) and 'sentence' in value}


def _navigation_pages():
    """{'area/page'} for every page navigation.tsx declares, read area by area."""
    text = (CONSOLE / 'navigation.tsx').read_text(encoding='utf-8')
    body = text[text.index('export const NAVIGATION'):]
    starts = [(match.start(), match.group(1)) for match in re.finditer(r"\{\s*id: '([a-z]+)', label: '[^']+', icon: "
                                                                          r"<\w+ />,\s*pages: \[", body)]
    pages = set()
    for index, (start, area) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else len(body)
        pages |= {f'{area}/{page}' for page in re.findall(r"\{ id: '([a-z-]+)', label:", body[start:end])}
    return pages


# -- the catalogue stays complete ----------------------------------------------------------------------------------

def test_every_entry_names_real_settings_pages_parts_and_an_evidence_level():
    keys = [mechanism.key for mechanism in mechanisms.CATALOGUE]
    assert len(keys) == len(set(keys))
    stages = {key for key, _ in mechanisms.STAGES}
    pages = _navigation_pages()
    assert {'costs/savings', 'recipients/list', 'providers/trunk'} <= pages, 'navigation.tsx was not read'
    screen = SAVINGS_SCREEN.read_text(encoding='utf-8')
    for mechanism in mechanisms.CATALOGUE:
        assert mechanism.stage in stages, mechanism.key
        assert mechanism.evidence in mechanisms.EVIDENCE, f'{mechanism.key} has no product evidence level'
        assert sorted(set(mechanism.settings) - set(ConfigurationValues.model_fields)) == [], mechanism.key
        assert mechanism.page in pages, f'{mechanism.key}: the console has no page {mechanism.page}'
        assert mechanism.name and mechanism.sentence.endswith('.') and mechanism.page_label, mechanism.key
        if mechanism.part is None:
            assert mechanisms.NO_PART.get(mechanism.key, '').strip(), f'{mechanism.key} has no Savings part or reason'
        else:
            assert mechanism.key not in mechanisms.NO_PART, f'{mechanism.key} has a part now: remove it from NO_PART'
            assert f'part="{mechanism.part}"' in screen, f'Savings.tsx renders no anchor for {mechanism.part}'
    assert sorted(set(mechanisms.NO_PART) - set(keys)) == []


def test_every_part_of_the_savings_page_has_a_catalogue_entry(installation):
    with installation.start(FAX_BACKEND='sip') as client:
        body = client.get('/routing/savings', headers=ADMIN).json()
    parts = _savings_parts(body)
    assert {'sending_together', 'direct_bytes', 'blocked_calls'} <= parts
    covered = {mechanism.part for mechanism in mechanisms.CATALOGUE}
    assert sorted(parts - covered) == [], 'Savings parts with no entry in routing/mechanisms.py'
    assert sorted(covered - parts - {None}) == [], 'Entries naming a Savings part that does not exist'


# -- a real installation -------------------------------------------------------------------------------------------

def _record(engine, *, job_id, route, reason, sequence=1, outcome='success', when=None):
    """One delivery attempt as Faxbot records it: the attempt, its route decision, then its outcome."""
    from app.routing.store import RouteStore
    routes, attempt, when = RouteStore(engine), uuid4().hex, when or datetime.utcnow()
    with engine.begin() as connection:
        connection.execute(routes.attempts.insert().values(
            id=attempt, job_id=job_id, sequence=sequence, phase='success' if outcome == 'success' else 'failed',
            created_at=when, submitted_at=when, completed_at=when))
    routes.record_decision(attempt_id=attempt, job_id=job_id, destination=AGREED, route=route, reason=reason,
                           provider_id=route)
    with engine.begin() as connection:
        connection.execute(routes.costs.update().where(routes.costs.c.id == attempt).values(
            outcome=outcome, created_at=when))
    return attempt


def _fax(client, to=AGREED):
    response = client.post('/fax', headers=ADMIN, data={'to': to},
                           files={'file': ('note.txt', b'Synthetic page\n', 'text/plain')})
    assert response.status_code in (200, 202), response.text
    return response.json()['id']


def _by_key(body):
    return {item['key']: item for stage in body['stages'] for item in stage['mechanisms']}


def scenario(installation):
    """A real installation with a Telnyx trunk, a recipient who shares calls, a blocked sender with one call turned
    away, a toll-free approval and faxes sent by their cheapest route: (map, savings) as the API answers them."""
    from app.inbound.screening import Rejection, ScreeningStore
    from app.routing.tollfree import TollFreeApprovals
    settings = dict(FAX_BACKEND='sip', SIP_TRUNK_PRESET='telnyx', SIP_TRUNK_DIDS=DID, SIP_TRUNK_CALLER_ID=DID,
                    INBOUND_ENABLED='true', FAX_FRIENDLY_DOCUMENTS='never')
    with installation.start(**settings) as client:
        engine = installation.store().engine
        # A recipient who agreed to share calls; Telnyx's shipped card bills whole minutes, so sharing saves.
        agreed = client.put(f'/batching/numbers/{AGREED}', headers=ADMIN, json={'enabled': True, 'recipient_agreed': True})
        assert agreed.status_code == 200, agreed.text
        # A blocked sender, and one call from it turned away.
        screening = ScreeningStore(engine)
        screening.add(JUNK, 'Synthetic junk sender', actor_name='Synthetic admin')
        assert screening.record_rejection(Rejection(f'{int(datetime.utcnow().timestamp())}.1', JUNK, DID, None,
                                                    datetime.utcnow() - timedelta(minutes=5)))
        # A recipient who approved a toll-free number.
        TollFreeApprovals(engine).record(TOLL_FREE_FOR, action='approved', alternate_number='+18005550100',
                                         approved_by='Synthetic clinic', approved_on=datetime.utcnow(),
                                         evidence='Synthetic letter')
        # Two faxes sent by their cheapest route per delivered fax, one failed (not counted), one too old.
        jobs = [_fax(client) for _ in range(3)]
        _record(engine, job_id=jobs[0], route='sip', reason='cheapest_delivered')
        _record(engine, job_id=jobs[1], route='sip', reason='cheapest_delivered')
        _record(engine, job_id=jobs[2], route='sip', reason='cheapest_delivered', outcome='failed')
        _record(engine, job_id=jobs[2], route='sip', reason='cheapest_delivered', sequence=2,
                when=datetime.utcnow() - timedelta(days=45))

        response = client.get('/routing/savings/mechanisms', headers=ADMIN)
        assert response.status_code == 200, response.text
        return response.json(), client.get('/routing/savings', headers=ADMIN).json()


def test_the_map_reads_a_real_installation_and_never_shows_money(installation):
    body, savings = scenario(installation)
    # The map never carries money: no amount, no currency, no "$".
    text = json.dumps(body)
    assert '$' not in text and 'USD' not in text and 'amount' not in text and 'micros' not in text
    assert [stage['title'] for stage in body['stages']] == [title for _, title in mechanisms.STAGES]
    assert body['title'] == 'How Faxbot saves money' and len(body['legend']) == 3
    items = _by_key(body)
    assert set(items) == {mechanism.key for mechanism in mechanisms.CATALOGUE}

    together = items['sending_together']
    assert (together['enabled']['on'], together['works']['here']) == (True, True)
    assert together['enabled']['sentence'] == 'On for 1 recipient who agreed.'
    assert together['evidence'] == {'level': 'lab', 'label': 'Proven in the test lab'}
    assert together['here'] == {'used': 0, 'sentence': 'Not used here in the last 30 days.'}
    assert together['turn_on'] is False

    blocked = items['blocked_senders']
    assert blocked['enabled'] == {'on': True, 'label': 'On', 'sentence': 'On for 1 number on your blocked list.'}
    assert blocked['works'] == {'here': True, 'label': 'Works here', 'sentence': None}
    assert blocked['here'] == {'used': 1, 'sentence': 'Turned away 1 call in the last 30 days.'}
    assert blocked['part'] == 'blocked_calls' and blocked['page'] == 'numbers/blocked'
    assert savings['blocked_calls']['sentence'] == '1 call from blocked senders was turned away before Faxbot answered.'

    assert items['toll_free']['enabled']['sentence'] == 'On for 1 recipient who approved a toll-free number.'

    cheapest = items['cheapest_route']
    assert cheapest['here'] == {'used': 2, 'sentence': 'Worked on 2 faxes in the last 30 days.'}
    assert savings['cheapest_route']['sentence'] == '2 faxes went by the route that cost least per delivered fax to their numbers.'
    assert savings['cheapest_route']['estimate'] is False and savings['cheapest_route']['saved'] == []
    # One sending route only: nothing to compare yet, said in one sentence that names it.
    assert cheapest['works'] == {'here': False, 'label': 'Not here',
                                 'sentence': 'Needs a second sending route to compare with; you send through '
                                             'Telnyx only.'}

    # Shading is off by the setting and the trunk bills by time, so the map offers to turn it on there.
    friendly = items['fax_friendly']
    assert (friendly['enabled']['label'], friendly['works']['label'], friendly['turn_on']) == ('Off', 'Works here', True)
    assert friendly['page'] == 'providers/sending' and friendly['page_label'] == 'Providers → In use → Delivery routes'

    # The trunk's own numbers receive into Faxbot, so faxes to them need no call.
    assert items['own_numbers']['enabled']['on'] is True and items['own_numbers']['works']['here'] is True

    # No partner: direct delivery is off and cannot work here, so there is nothing to turn on yet.
    direct = items['direct_delivery']
    assert direct['works'] == {'here': False, 'label': 'Not here',
                               'sentence': 'Needs a verified partner; you have none yet.'}
    assert direct['enabled']['on'] is False and direct['turn_on'] is False

    sslfax = items['sslfax']
    assert sslfax['page_label'] == 'Providers → Telnyx' and sslfax['evidence']['level'] == 'lab'
    # The fast fax service starts only after Apply and connect, which this installation never ran.
    assert sslfax['works']['here'] is False and 'Apply and connect' in sslfax['works']['sentence']

    busy = items['busy_hours']
    assert busy['part'] is None and busy['here']['sentence'] == mechanisms.NO_PART['busy_hours']
    for item in items.values():
        assert item['evidence']['level'] in mechanisms.EVIDENCE and item['here']['sentence'].endswith('.')
        # "Turn on" only for something off that works here.
        assert not item['turn_on'] or (not item['enabled']['on'] and item['works']['here']), item['key']


def test_the_command_and_the_console_show_the_same_sentences_as_the_server():
    """savingsMap.json is one evaluated answer: the command prints exactly its lines, the console's map test renders
    the same answer, and its catalogue fields are the catalogue's own, so neither surface can drift."""
    from app.cli.commands.delivery import mechanism_lines
    fixture = json.loads(FIXTURE.read_text(encoding='utf-8'))
    response = fixture['response']
    assert mechanism_lines(response) == fixture['cli']
    assert (response['title'], response['sentence']) == (mechanisms.TITLE, mechanisms.SENTENCE)
    assert [(entry['label'], entry['sentence']) for entry in response['legend']] == list(mechanisms.LEGEND)
    assert [(stage['key'], stage['title']) for stage in response['stages']] == list(mechanisms.STAGES)
    listed = [(stage['key'], item['key'], item['name'], item['sentence'], item['evidence']['label'], item['part'],
               item['page']) for stage in response['stages'] for item in stage['mechanisms']]
    assert listed == [(m.stage, m.key, m.name, m.sentence, mechanisms.EVIDENCE[m.evidence], m.part, m.page)
                      for key, _ in mechanisms.STAGES for m in mechanisms.CATALOGUE if m.stage == key]


def test_faxbot_costs_mechanisms_prints_the_map_by_stage(cli):
    result = cli('costs', 'mechanisms')
    assert result.exit_code == 0, result.stdout
    lines = result.stdout.splitlines()
    assert lines[0] == 'How Faxbot saves money' and lines[-1] == 'What each one saved: faxbot costs savings'
    for _, title in mechanisms.STAGES:
        assert title in lines
    assert '  Sending together: Off · Not here · Proven in the test lab' in lines
    assert '    Needs your own SIP trunk; you send through Phaxio only.' in lines
    assert '$' not in result.stdout
    body = cli.json('costs', 'mechanisms')
    assert [stage['title'] for stage in body['stages']] == [title for _, title in mechanisms.STAGES]


def test_the_map_needs_settings_read(installation):
    with installation.start(FAX_BACKEND='sip') as client:
        made = client.post('/admin/api-keys', headers=ADMIN, json={'name': 'synthetic', 'scopes': ['fax:send']})
        assert made.status_code == 200, made.text
        refused = client.get('/routing/savings/mechanisms', headers={'X-API-Key': made.json()['token']})
        assert refused.status_code == 403
