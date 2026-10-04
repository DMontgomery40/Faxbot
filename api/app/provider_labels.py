"""One plain name for each fax provider, wherever Faxbot shows one.

The console keeps the same names in ``api/admin_ui/src/providerLabels.ts``;
``api/tests/test_provider_labels.py`` keeps the two lists equal.
"""

PROVIDER_LABELS = {
    'phaxio': 'Phaxio',
    'sinch': 'Sinch',
    'signalwire': 'SignalWire',
    'documo': 'Documo',
    'humblefax': 'HumbleFax',
    'sip': 'SIP trunk (Asterisk)',
    'freeswitch': 'SIP trunk (FreeSWITCH)',
}

NO_PROVIDER = 'No provider'


def provider_label(identity, plugin_name=None):
    """The plain name for a provider id; a plugin's own name for ids Faxbot does not know."""
    key = str(identity or '').strip().lower()
    if not key:
        return NO_PROVIDER
    return PROVIDER_LABELS.get(key) or (str(plugin_name).strip() if plugin_name else '') or key
