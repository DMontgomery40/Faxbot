"""One plain name for each fax provider, wherever Faxbot shows one.

The console keeps the same names in ``api/admin_ui/src/providerLabels.ts``;
``api/tests/test_provider_labels.py`` keeps the two lists equal.

The fax line Faxbot runs itself is named after the carrier or phone system it
connects to ("Telnyx", "Avaya IP Office"), never after the fax engine; "Carrier
trunk" is the name only while no carrier is chosen.
"""

PROVIDER_LABELS = {
    'phaxio': 'Phaxio',
    'sinch': 'Sinch',
    'signalwire': 'SignalWire',
    'documo': 'Documo',
    'humblefax': 'HumbleFax',
    'efax': 'eFax',
    'sip': 'Carrier trunk',
    'freeswitch': 'FreeSWITCH',
}

NO_PROVIDER = 'No provider'
# The trunk preset for a carrier Faxbot has no preset for.
OTHER_CARRIER = 'Your carrier'


def trunk_name(preset=None):
    """The carrier or phone system the trunk connects to, by name; the active one when ``preset`` is None."""
    if preset is None:
        try:
            from .config import configuration_values
            preset = configuration_values().sip_trunk_preset
        except Exception:  # configuration not loaded yet: the general name
            preset = ''
    from .sip_trunk import PRESETS
    found = PRESETS.get(str(preset or '').strip())
    if found is None:
        return PROVIDER_LABELS['sip']
    return OTHER_CARRIER if found.id == 'custom' else found.label


def provider_label(identity, plugin_name=None):
    """The plain name for a provider id; a plugin's own name for ids Faxbot does not know."""
    key = str(identity or '').strip().lower()
    if not key:
        return NO_PROVIDER
    if key == 'sip':
        return trunk_name()
    return PROVIDER_LABELS.get(key) or (str(plugin_name).strip() if plugin_name else '') or key
