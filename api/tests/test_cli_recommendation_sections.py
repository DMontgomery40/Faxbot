"""The CLI's complete recommendation report includes partner discovery and relay advice."""
import json
import httpx
from typer.testing import CliRunner
from app.cli.main import app


def test_complete_report_keeps_partner_discovery_and_relay_sections():
    def handle(request):
        if request.url.path == '/direct/discovery':
            return httpx.Response(200, json={'suggestions': [{'sentence': 'A verified partner candidate was found.'}]})
        if request.url.path == '/direct/relay/recommendations':
            return httpx.Response(200, json={'recommendations': [{'sentence': 'A partner route costs less.', 'action': 'Review the agreement.'}]})
        return httpx.Response(200, json={})
    with httpx.Client(transport=httpx.MockTransport(handle), base_url='https://faxbot.example') as client:
        result = CliRunner().invoke(app, ['--url', 'https://faxbot.example', '--key', 'synthetic', '--json',
                                         'costs', 'recommendations'], obj={'client_factory': lambda *_: (client, False)})
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report['discovery']['suggestions'][0]['sentence'] == 'A verified partner candidate was found.'
    assert report['relays']['recommendations'][0]['action'] == 'Review the agreement.'


def test_complete_report_includes_missing_facts_from_shared_console_fixture():
    from pathlib import Path
    facts = json.loads((Path(__file__).parents[1] / 'admin_ui/src/__tests__/opportunityFacts.json').read_text())
    requests = []

    def handle(request):
        requests.append((request.method, request.url.path))
        return httpx.Response(200, json=facts if request.url.path == '/routing/recommendations/facts' else {})

    with httpx.Client(transport=httpx.MockTransport(handle), base_url='https://faxbot.example') as client:
        result = CliRunner().invoke(app, ['--url', 'https://faxbot.example', '--key', 'synthetic', '--json',
                                         'savings', 'opportunities'], obj={'client_factory': lambda *_: (client, False)})
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)['facts'] == facts
    assert ('GET', '/routing/recommendations/facts') in requests
    assert all(method == 'GET' for method, _ in requests)


def test_missing_fact_words_match_the_shared_console_fixture():
    from pathlib import Path
    from app.cli.commands.fact_advice import show_facts
    facts = json.loads((Path(__file__).parents[1] / 'admin_ui/src/__tests__/opportunityFacts.json').read_text())

    class Output:
        def __init__(self):
            self.lines = []

        def line(self, value=''):
            self.lines.append(value)

    out = Output()
    show_facts(out, facts)
    text = '\n'.join(out.lines)
    row = facts['recipients'][0]['facts'][0]
    for sentence in (facts['sentence'], facts['realized'], facts['assumptions'][0], row['sentence'],
                     row['confirm'], row['step'], 'Evidence period: last 90 days',
                     'Expected and cautious benefit: unknown', f"Effort: {row['establish']}"):
        assert sentence in text
    assert 'Apply' not in text
