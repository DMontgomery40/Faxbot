"""Captured manifest readiness is a pure configuration check, not a provider probe."""
from copy import deepcopy

import pytest

from api.app.plugins.http_provider import HttpManifest, HttpProviderRuntime
from api.app.provider_catalog import validate_http_provider_document


def runtime(*, auth=None, credentials=None, settings=None, url='https://provider.invalid/faxes',
            headers=None, body='', path_params=None, kind='json', domains=None):
    document = {
        'id': 'synthetic-provider', 'auth': auth or {'scheme': 'none'},
        'allowed_domains': ['provider.invalid'] if domains is None else domains,
        'actions': {'send_fax': {'method': 'POST', 'url': url,
            'headers': {} if headers is None else headers,
            'body': {'kind': kind, 'template': body},
            'path_params': [] if path_params is None else path_params}},
    }
    return HttpProviderRuntime(HttpManifest.from_dict(document), credentials, settings)


@pytest.mark.parametrize('auth,credentials', [
    ({'scheme': 'none'}, {}),
    ({'scheme': 'basic'}, {'username': 'account', 'api_key': 'key'}),
    ({'scheme': 'basic'}, {'username': 'account', 'password': 'password'}),
    ({'scheme': 'bearer'}, {'api_key': 'key'}),
    ({'scheme': 'BEARER'}, {'api_key': '', 'token': 'token'}),
    ({'scheme': 'api_key_header'}, {'api_key': 'key'}),
    ({'scheme': 'api_key_query'}, {'api_key': 'key'}),
])
def test_supported_auth_uses_captured_credentials(auth, credentials):
    assert runtime(auth=auth, credentials=credentials).is_configured() is True


@pytest.mark.parametrize('auth,credentials', [
    ({'scheme': 'unsupported'}, {'api_key': 'key'}),
    ({'scheme': 'basic'}, {'username': 'account'}),
    ({'scheme': 'basic'}, {'username': '   ', 'api_key': 'key'}),
    ({'scheme': 'basic'}, {'username': 'account', 'api_key': '', 'password': 'fallback'}),
    ({'scheme': 'basic'}, {'username': 'account', 'api_key': None, 'password': 'fallback'}),
    ({'scheme': 'bearer'}, {'api_key': '   ', 'token': 'fallback'}),
    ({'scheme': 'bearer'}, {}),
    ({'scheme': 'api_key_header'}, {'api_key': '\t\n'}),
    ({'scheme': 'api_key_query'}, {'api_key': None}),
    ({'scheme': 'bearer'}, {'api_key': 123}),
    ({'scheme': 'basic'}, {'username': ['account'], 'password': 'password'}),
])
def test_unusable_auth_and_runtime_fallback_do_not_claim_readiness(auth, credentials):
    assert runtime(auth=auth, credentials=credentials).is_configured() is False


@pytest.mark.parametrize('location', ['url', 'headers', 'body', 'path_params'])
@pytest.mark.parametrize('value', [None, '', '   ', {}, [], False, 123])
def test_missing_or_malformed_referenced_credentials_refuse_readiness(location, value):
    options = {'credentials': {'account': value}}
    if location == 'url':
        options['url'] = 'https://provider.invalid/accounts/{{creds.account}}/faxes'
    elif location == 'headers':
        options['headers'] = {'X-Account': '{{creds.account}}'}
    elif location == 'body':
        options['body'] = '{"account":"{{creds.account}}"}'
    else:
        options['url'] = 'https://provider.invalid/accounts/{account}/faxes'
        options['path_params'] = [{'name': 'account', 'source': 'creds.account'}]
    assert runtime(**options).is_configured() is False


def test_nested_captured_references_render_before_allowlist_check():
    assert runtime(url='https://{{settings.endpoint.host}}/accounts/{account}/faxes',
        credentials={'identity': {'account': 'captured-account'}},
        settings={'endpoint': {'host': 'provider.invalid'}},
        path_params=[{'name': 'account', 'source': 'creds.identity.account'}],
        headers={'X-Account': '{{creds.identity.account}}'}).is_configured() is True


@pytest.mark.parametrize('settings', [{}, {'account': None}, {'account': ''}, {'account': '  '},
                                      {'account': []}, {'account': {}}])
@pytest.mark.parametrize('path_parameter', [False, True])
def test_missing_captured_url_setting_is_required(settings, path_parameter):
    url = 'https://provider.invalid/{{settings.account}}/faxes'
    parameters = []
    if path_parameter:
        url = 'https://provider.invalid/{account}/faxes'
        parameters = [{'name': 'account', 'source': 'settings.account'}]
    assert runtime(url=url, settings=settings, path_params=parameters).is_configured() is False


@pytest.mark.parametrize('value', [0, False])
def test_zero_and_false_url_settings_remain_usable(value):
    assert runtime(url='https://provider.invalid/{{settings.account}}/faxes',
                   settings={'account': value}).is_configured() is True


def test_optional_non_url_settings_and_per_job_fields_do_not_require_a_job():
    assert runtime(url='https://provider.invalid/faxes/{job}/{{attempt_id}}?to={{to}}',
        path_params=[{'name': 'job', 'source': 'job_id'}],
        headers={'X-Optional': '{{settings.optional}}', 'X-From': '{{from}}'},
        body='{"to":"{{to}}","url":"{{file_url}}","tag":"{{settings.optional}}"}').is_configured() is True
    assert runtime(kind='multipart', body='attachment={{file}}&from={{from}}').is_configured() is True


@pytest.mark.parametrize('url', [
    '', 'ftp://provider.invalid/fax', '/fax', 'https:///fax',
    'https://provider.invalid:bad/fax', 'https://provider.invalid:65536/fax',
    'https://[broken/fax', 'https://provider.invalid/\nprivate',
    'https://provider.invalid/ private', 'https://other.invalid/fax',
    'https://provider.invalid/{{creds.missing}}',
    'https://provider.invalid/{{ settings.account', 'https://provider.invalid/{unmapped}',
    'https://provider.invalid/{{unknown}}', 'https://provider.invalid/{{settings..account}}',
])
def test_unusable_endpoint_or_template_is_not_ready(url):
    assert runtime(url=url, settings={'account': 'captured'}).is_configured() is False


@pytest.mark.parametrize('options', [
    {'headers': {'X-Token': '{{ creds.api_key '}},
    {'body': '{"token":"{{creds.api_key"}'},
    {'headers': {'X-Token': 123}},
    {'body': ['invalid']},
    {'url': ['invalid']},
    {'path_params': [{'name': 'account', 'source': ['invalid']}]},
    {'path_params': [None]},
    {'body': '{"token":"{{creds}}"}'},
    {'headers': {'X-Token': '{{creds.api_key}}'}, 'credentials': {'api_key': 'secret\r\ninjected'}},
])
def test_malformed_runtime_templates_fail_closed(options):
    assert runtime(**options).is_configured() is False


def test_send_action_is_required_and_empty_allowlist_preserves_catalog_contract():
    instance = runtime(domains=[])
    assert instance.is_configured() is True
    del instance.m.actions['send_fax']
    assert instance.is_configured() is False


@pytest.mark.parametrize('auth', [
    {'scheme': 'api_key_header', 'header_name': ''},
    {'scheme': 'api_key_query', 'query_name': 123},
    {'scheme': 'api_key_query', 'query_name': ''},
    {'scheme': 'api_key_query', 'query_name': None},
])
def test_auth_parameter_names_must_match_validated_manifest_shape(auth):
    assert runtime(auth=auth, credentials={'api_key': 'captured'}).is_configured() is False


@pytest.mark.parametrize('method', [123, '', 'UNSUPPORTED'])
def test_malformed_send_method_cannot_be_ready(method):
    instance = runtime()
    instance.m.actions['send_fax'].method = method
    assert instance.is_configured() is False


def test_readiness_never_opens_files_or_network_and_does_not_mutate_or_log(monkeypatch, caplog):
    document = {'id': 'synthetic-provider', 'auth': {'scheme': 'api_key_header'},
        'allowed_domains': ['provider.invalid'], 'actions': {'send_fax': {
            'url': 'https://provider.invalid/faxes', 'headers': {'X-Key': '{{creds.api_key}}'},
            'body': {'kind': 'json', 'template': '{"account":"{{settings.account}}"}'}}}}
    validate_http_provider_document(document)
    instance = HttpProviderRuntime(HttpManifest.from_dict(document), {'api_key': 'private-synthetic'}, {})
    before = deepcopy(instance.__dict__)
    def forbidden(*args, **kwargs):
        pytest.fail('readiness performed I/O')
    monkeypatch.setattr('httpx.AsyncClient', forbidden)
    monkeypatch.setattr('builtins.open', forbidden)
    assert instance.is_configured() is True
    assert instance.__dict__ == before
    assert caplog.text == ''
