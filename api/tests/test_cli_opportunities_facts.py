"""Missing-fact Opportunities parity uses the console's synthetic fixture; no network."""
import json
from pathlib import Path

import httpx

from app.cli.commands import delivery
from api.tests.test_cli_analysis import invoke

FIXTURE = json.loads((Path(__file__).parents[1] / 'admin_ui/src/__tests__/opportunitiesFacts.json').read_text())


def test_aggregate_includes_the_shared_fact_advice(monkeypatch):
    # Other domains have independent tests. Only their reads are replaced here, keeping
    # the aggregate callback, HTTP fact read, renderer and CLI serialization real.
    monkeypatch.setattr(delivery, 'RECOMMENDATION_SECTIONS', [
        row for row in delivery.RECOMMENDATION_SECTIONS if row[0] == 'facts'])
    calls = []

    def handle(request):
        calls.append((request.method, request.url.path))
        assert request.url.path == '/routing/recommendations/facts'
        return httpx.Response(200, json=FIXTURE['advice'])

    result = invoke('--json', 'savings', 'opportunities', handler=handle)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)['facts'] == FIXTURE['advice']
    assert calls == [('GET', '/routing/recommendations/facts')]


def test_facts_command_prints_shared_evidence_prerequisite_and_effort():
    result = invoke('savings', 'opportunities', 'facts', handler=lambda request:
                    httpx.Response(200, json=FIXTURE['advice']))
    assert result.exit_code == 0, result.output
    text = ' '.join(result.stdout.split())
    advice = FIXTURE['advice']
    assert 'Evidence period: last 90 days.' in text
    assert 'Expected benefit: unknown.' in text
    assert 'Cautious benefit: unknown.' in text
    for recipient in advice['recipients']:
        for row in recipient['facts']:
            for key in ('sentence', 'confirm', 'step'):
                assert row[key] in text
