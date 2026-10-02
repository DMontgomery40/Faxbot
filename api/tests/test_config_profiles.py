"""Immutable snapshots never share mutable credential or manifest objects."""
import pytest

from api.app.config_profiles import ConfigurationDocument, ConfigurationRecordError, ProviderConfiguration


def test_provider_snapshot_does_not_expose_mutable_secret_state():
    credentials = {'api_key': 'private', 'nested': {'items': ['original']}}
    profile = ProviderConfiguration('example', credentials=credentials, manifest={'id': 'example'})
    credentials['nested']['items'][0] = 'mutated'
    copy = profile.credentials
    copy['nested']['items'].clear()
    assert profile.credentials['nested']['items'] == ['original']
    assert 'private' not in repr(profile)
    assert 'private' not in repr(ConfigurationDocument({'secret': 'private'}))
    assert ProviderConfiguration.from_payload(profile.as_dict()) == profile


@pytest.mark.parametrize('payload', [{1: 'coercion'}, {'x': float('nan')}, {'x': b'private'},
    {'x': ('not', 'json')}, {'x': '\ud800'}, {'x': 'x' * (1024 * 1024)}])
def test_invalid_private_json_is_rejected_without_disclosing_values(payload):
    with pytest.raises(ConfigurationRecordError) as caught:
        ConfigurationDocument(payload)
    assert str(caught.value) == 'Invalid or oversized configuration record.'


@pytest.mark.parametrize('identity', ['../escape', '', 'p' * 256, 'line\nfeed', None])
def test_invalid_provider_identity_is_refused(identity):
    with pytest.raises(ConfigurationRecordError):
        ProviderConfiguration(identity)


def test_manifest_must_belong_to_the_selected_provider():
    with pytest.raises(ConfigurationRecordError):
        ProviderConfiguration('example', manifest={'id': 'different'})
