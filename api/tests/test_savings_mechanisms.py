"""The savings map: one catalogue entry for every way Faxbot saves money, evaluated for a real installation.

The catalogue (app/routing/mechanisms.py) must stay complete, so this fails when:
- a part of GET /routing/savings has no catalogue entry;
- an entry names a setting that is not a configuration value, a console page navigation.tsx does not have, or a
  Savings part (or Recommendations section) the console does not render with its anchor;
- an entry has no product evidence level, or has neither a Savings part, nor a page with its own figures, nor a
  recorded reason;
- a section of Costs → Recommendations has no advice card.

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
RECOMMENDATIONS_SCREEN = CONSOLE / 'components' / 'delivery' / 'Recommendations.tsx'
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
    advice = RECOMMENDATIONS_SCREEN.read_text(encoding='utf-8')
    reference = (API_ROOT.parent / 'docs' / 'reference' / 'cli.md').read_text(encoding='utf-8')
    for mechanism in mechanisms.CATALOGUE:
        assert mechanism.stage in stages, mechanism.key
        assert mechanism.evidence in mechanisms.EVIDENCE, f'{mechanism.key} has no product evidence level'
        assert sorted(set(mechanism.settings) - set(ConfigurationValues.model_fields)) == [], mechanism.key
        assert mechanism.page in pages, f'{mechanism.key}: the console has no page {mechanism.page}'
        assert mechanism.name and mechanism.sentence.endswith('.') and mechanism.page_label, mechanism.key
        if mechanism.part is not None:
            assert mechanism.key not in mechanisms.NO_PART, f'{mechanism.key} has a part now: remove it from NO_PART'
            assert mechanism.link is None, f'{mechanism.key} leads to its Savings part, not elsewhere'
            assert f'part="{mechanism.part}"' in screen, f'Savings.tsx renders no anchor for {mechanism.part}'
        elif mechanism.link is not None:
            # Advice and charge checks lead to the page that already has their figures, never to Savings.
            page, _, query = mechanism.link.partition('?')
            assert page in pages, f'{mechanism.key}: the console has no page {page}'
            assert mechanism.link_label and mechanism.command and mechanism.here, mechanism.key
            assert f'### `{mechanism.command}`' in reference, f'{mechanism.key}: no command {mechanism.command}'
            if page == 'costs/recommendations':
                section = query.removeprefix('section=')
                assert f'section="{section}"' in advice, f'Recommendations.tsx renders no anchor for {section}'
        else:
            assert mechanisms.NO_PART.get(mechanism.key, '').strip(), f'{mechanism.key} has no Savings part or reason'
        if mechanism.stage == 'advice':
            assert mechanism.part is None and mechanism.link, f'{mechanism.key}: advice saves nothing until acted on'
    assert sorted(set(mechanisms.NO_PART) - set(keys)) == []
    # Every Recommendations section has its card.
    sections = set(re.findall(r'<Anchor section="([A-Za-z]+)"', advice))
    linked = {m.link.partition('?section=')[2] for m in mechanisms.CATALOGUE if m.link and '?section=' in m.link}
    assert sections and sorted(sections - linked) == [], 'Recommendations sections with no card on the savings map'


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
        # Ten answered trunk calls over fax over IP (18 seconds a page) and ten over audio fax (30 seconds a page),
        # and one T.38 call that was never answered (not counted).
        calls = sa.Table('sip_call_records', sa.MetaData(), autoload_with=engine)
        start = datetime.utcnow() - timedelta(days=2)
        with engine.begin() as connection:
            for index, (t38, seconds, answered) in enumerate([('yes', 36, True)] * 10 + [('no', 60, True)] * 10
                                                              + [('yes', None, False)]):
                at = start + timedelta(minutes=index)
                connection.execute(calls.insert().values(
                    id=uuid4().hex, direction='outbound', call_id=f'synthetic-{index}', trunk_preset='telnyx',
                    caller=DID, called=AGREED, started_at=at, answered_at=at if answered else None, ended_at=at,
                    disposition='answered' if answered else 'no_answer', connected_seconds=seconds, t38=t38,
                    pages=2 if answered else None, fax_status='SUCCESS' if answered else None, fax_preference=1,
                    created_at=at, updated_at=at))

        response = client.get('/routing/savings/mechanisms', headers=ADMIN)
        assert response.status_code == 200, response.text
        return response.json(), client.get('/routing/savings', headers=ADMIN).json()


def test_the_map_reads_a_real_installation_and_never_shows_money(installation):
    body, savings = scenario(installation)
    # The map never carries money: no amount, no currency, no "$".
    text = json.dumps(body)
    assert '$' not in text and 'USD' not in text and '"amount"' not in text and 'micros' not in text
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

    # Fax over IP is the one proven on a live call; Savings compares measured seconds a page, no money.
    t38 = items['fax_over_ip']
    assert (t38['enabled']['on'], t38['works']['here'], t38['evidence']['level']) == (True, True, 'live')
    assert t38['here']['sentence'] == 'Worked on 10 calls in the last 30 days.'
    assert t38['link'] == 'costs/savings?part=t38' and t38['command'] == 'faxbot costs savings'
    assert savings['t38']['sentence'] == ('Fax over IP (T.38) took about 18 seconds a page over 10 calls, and audio '
                                          'fax about 30 seconds a page over 10 calls.')
    assert savings['t38']['saved'] == [] and savings['t38']['estimate'] is False

    busy = items['busy_hours']
    assert busy['part'] is None and busy['here']['sentence'] == mechanisms.NO_PART['busy_hours']
    assert busy['link'] is None and items['free_line']['link'] is None

    # Advice leads to its own section of Recommendations, never to Savings, and is always on.
    plans = items['advice_plans']
    assert plans['link'] == 'costs/recommendations?section=plans' and plans['part'] is None
    assert plans['enabled']['on'] is True and plans['turn_on'] is False
    assert plans['works'] == {'here': False, 'label': 'Not here',
                              'sentence': 'Needs a monthly plan; none of your fax services has one.'}
    assert plans['here']['sentence'] == mechanisms.ADVICE_HERE
    assert items['advice_billing_steps']['works']['here'] is True  # the trunk
    assert items['advice_caller_names']['works']['sentence'] == 'Needs your Telnyx key saved on the Telnyx page.'
    charges = items['charge_checks']
    assert (charges['link'], charges['link_label'], charges['command']) == (
        'costs/charges', 'Costs → Charges', 'faxbot costs charges')
    stages = {stage['key']: stage for stage in body['stages']}
    assert [key for key, stage in stages.items() if not stage['path']] == ['advice']
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
               item['page'], item['link']) for stage in response['stages'] for item in stage['mechanisms']]
    assert listed == [(m.stage, m.key, m.name, m.sentence, mechanisms.EVIDENCE[m.evidence], m.part, m.page,
                       m.destination)
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
