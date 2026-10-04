"""One name per fax provider, the same in the console, the command line and the plugin list."""
import json
from pathlib import Path
import re

from app.provider_labels import PROVIDER_LABELS, provider_label
from app.routing.plan import route_label


ROOT = Path(__file__).resolve().parents[2]


def _console_labels():
    text = (ROOT / 'api' / 'admin_ui' / 'src' / 'providerLabels.ts').read_text()
    block = re.search(r'export const PROVIDER_LABELS: Record<string, string> = \{(.*?)\};', text, re.S).group(1)
    return dict(re.findall(r"(\w+): '([^']+)'", block))


def test_console_and_server_use_the_same_names():
    assert _console_labels() == PROVIDER_LABELS
    assert PROVIDER_LABELS['sip'] == 'SIP trunk (Asterisk)'


def test_built_in_plugins_are_listed_under_the_same_names():
    registry = json.loads((ROOT / 'config' / 'plugin_registry.json').read_text())
    entries = registry['items']
    names = {entry['id']: entry['name'] for entry in entries if entry['id'] in PROVIDER_LABELS}
    assert names and all(name == PROVIDER_LABELS[identity] for identity, name in names.items())


def test_routes_and_unknown_providers():
    assert route_label('sip') == 'SIP trunk (Asterisk)'
    assert route_label('direct') == 'Direct delivery'
    assert provider_label('') == 'No provider'
    assert provider_label('SIP') == 'SIP trunk (Asterisk)'
    assert provider_label('acme-fax', 'Acme Fax') == 'Acme Fax'
