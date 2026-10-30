"""Guidance reads local facts; it never activates or sends anything."""
import json
from pathlib import Path

from app.routing.capability_setup import dense_pages_guide
from app.cli.commands.capabilities import setup_lines


def test_dense_pages_guide_has_seven_steps_and_reports_saved_route_facts():
    routes = [{'route': 'sinch', 'label': 'Sinch', 'long_pages': False,
               'sentence': 'Off until you check long pages arrive unchanged.'}]
    guide = dense_pages_guide(routes)
    assert [step['key'] for step in guide['steps']] == [
        'explain', 'prerequisites', 'edit', 'review', 'save', 'verify', 'status']
    assert guide['routes'] == routes
    assert guide['steps'][2]['address'] == 'delivery/connections'
    assert 'settings:write' in guide['steps'][1]['sentence']
    assert 'queued' in guide['steps'][3]['sentence']
    assert 'immediately' in guide['steps'][4]['sentence']
    assert 'does not prove' in guide['steps'][5]['sentence']
    assert 'not undo' in guide['cancel']
    assert 'agreement' in guide['steps'][1]['sentence']


def test_command_uses_shared_guidance_and_exposes_saved_state_without_mutations():
    guide = dense_pages_guide([])
    fixture = json.loads((Path(__file__).parents[1] / 'admin_ui/src/__tests__/capabilities.json').read_text())
    item = next(item for group in fixture['response']['outcomes'] for item in group['capabilities']
                if item['key'] == 'dense_pages')
    item = {**item, 'setup': guide}
    lines = setup_lines(item)
    for step in guide['steps']:
        assert step['sentence'] in lines
    assert guide['cancel'] in lines
    assert 'No delivery routes are configured.' in lines
    assert json.loads(json.dumps(guide)) == guide
