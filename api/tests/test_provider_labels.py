"""One name per fax provider, the same in the console, the command line and the plugin list."""
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
    assert PROVIDER_LABELS['sip'] == 'Carrier trunk'


def test_routes_and_unknown_providers():
    assert route_label('direct') == 'Direct delivery'
    assert provider_label('') == 'No provider'
    assert provider_label('acme-fax', 'Acme Fax') == 'Acme Fax'
    assert provider_label('freeswitch') == 'FreeSWITCH'


def test_the_trunk_is_named_after_its_carrier_or_phone_system(monkeypatch):
    """Never the fax engine's name: the carrier or phone system the trunk connects to."""
    from app import config
    from app.config_values import ConfigurationValues
    from app.provider_labels import trunk_name
    from app.sip_trunk import PRESETS
    assert trunk_name('telnyx') == 'Telnyx'
    assert trunk_name('avaya-ipoffice') == 'Avaya IP Office'
    assert trunk_name('bt-one-voice') == 'BT One Voice'
    assert trunk_name('custom') == 'Your carrier'
    assert trunk_name('') == 'Carrier trunk'
    assert all(trunk_name(preset) for preset in PRESETS)
    values = ConfigurationValues.from_environment({'SIP_TRUNK_PRESET': 'gamma'})
    monkeypatch.setattr(config, 'configuration_values', lambda: values)
    assert provider_label('SIP') == 'Gamma' and route_label('sip') == 'Gamma'
