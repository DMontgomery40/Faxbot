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
