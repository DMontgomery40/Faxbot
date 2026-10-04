"""The editor sees desired values without disclosing stored credentials."""
import json
import os

import pytest

from api.app.config_store import ConfigurationRevision, ConfigurationSnapshot
from api.app.config_values import ConfigurationValues


def snapshot(environment=None, *, pending=None):
    active = ConfigurationRevision('active-revision', ConfigurationValues.from_environment(environment or {}))
    desired = ConfigurationRevision('desired-revision', ConfigurationValues.from_environment(pending)) if pending is not None else None
    return ConfigurationSnapshot('installation', 7, active, desired)


def test_editor_projects_whole_desired_candidate_and_identifies_pending_restart():
    from api.app.config_views import project_admin_settings

    frame = snapshot({'FAX_BACKEND': 'phaxio', 'FAX_DISABLED': 'true', 'ENABLE_MCP_HTTP': 'false'},
                     pending={'FAX_BACKEND': 'sinch', 'FAX_DISABLED': 'false', 'ENABLE_MCP_HTTP': 'true'})
    fields = ['enable_mcp_http']
    view = project_admin_settings(frame, pending_fields=fields)
    assert view['backend'] == {'type': 'sinch', 'disabled': False}
    assert view['mcp']['http_enabled'] is True
    assert view['_meta'] == {
        'active_revision_id': 'active-revision', 'desired_revision_id': 'desired-revision',
        'generation': 7, 'apply_state': 'pending_restart', 'pending_fields': ['enable_mcp_http'], 'env_managed': [],
    }
    assert frame.active.values.fax_backend == 'phaxio'
    assert frame.active.values.fax_disabled is True
    fields.append('audit_log_file')
    view['backend']['type'] = 'mutated-view'
    view['_meta']['pending_fields'].append('mutated-view')
    fresh = project_admin_settings(frame, pending_fields=('enable_mcp_http',))
    assert fresh['backend']['type'] == 'sinch'
    assert fresh['_meta']['pending_fields'] == ['enable_mcp_http']


@pytest.mark.parametrize(('environment', 'expected'), [
    ({'FAX_BACKEND': 'sip'}, {
        'outbound_backend': 'sip', 'inbound_backend': 'sip',
        'outbound_override': '', 'inbound_override': '', 'outbound_explicit': False, 'inbound_explicit': False,
    }),
    ({'FAX_BACKEND': 'phaxio', 'FAX_OUTBOUND_BACKEND': ' sinch ', 'FAX_INBOUND_BACKEND': ''}, {
        'outbound_backend': 'sinch', 'inbound_backend': 'phaxio',
        'outbound_override': 'sinch', 'inbound_override': '', 'outbound_explicit': True, 'inbound_explicit': False,
    }),
    ({'FAX_BACKEND': 'documo', 'FAX_INBOUND_BACKEND': 'sip'}, {
        'outbound_backend': 'documo', 'inbound_backend': 'sip',
        'outbound_override': '', 'inbound_override': 'sip', 'outbound_explicit': False, 'inbound_explicit': True,
    }),
])
def test_hybrid_effective_and_raw_selection_come_from_values_not_process_environment(monkeypatch, environment, expected):
    from api.app.config_views import project_admin_settings

    monkeypatch.setenv('FAX_BACKEND', 'unrelated-provider')
    monkeypatch.setenv('FAX_OUTBOUND_BACKEND', 'unrelated-outbound')
    monkeypatch.setenv('FAX_INBOUND_BACKEND', 'unrelated-inbound')
    before = dict(os.environ)
    view = project_admin_settings(snapshot(environment))
    assert view['hybrid'] == expected
    assert view['_meta'] == {
        'active_revision_id': 'active-revision', 'desired_revision_id': 'active-revision',
        'generation': 7, 'apply_state': 'applied', 'pending_fields': [], 'env_managed': [],
    }
    assert os.environ == before


def test_empty_credentials_are_empty_and_all_nonempty_credentials_have_opaque_masks():
    from api.app.config_views import project_admin_settings

    environment = {
        'API_KEY': 'synthetic-admin-key', 'PHAXIO_API_KEY': 'k', 'PHAXIO_API_SECRET': 'synthetic-phaxio-secret',
        'SINCH_API_KEY': 'synthetic-sinch-key', 'SINCH_API_SECRET': 'synthetic-sinch-secret',
        'DOCUMO_API_KEY': 'synthetic-documo-key', 'SIGNALWIRE_API_TOKEN': 'synthetic-signalwire-token',
        'HUMBLEFAX_ACCESS_KEY': 'synthetic-humblefax-access', 'HUMBLEFAX_SECRET_KEY': 'synthetic-humblefax-secret',
        'SIGNALWIRE_WEBHOOK_SIGNING_KEY': 'synthetic-webhook-key', 'ASTERISK_AMI_PASSWORD': 'synthetic-ami-password',
        'FREESWITCH_ESL_PASSWORD': 'synthetic-esl-password', 'ASTERISK_INBOUND_SECRET': 'synthetic-inbound-secret',
        'SINCH_INBOUND_BASIC_PASS': 'synthetic-basic-password', 'SINCH_INBOUND_HMAC_SECRET': 'synthetic-hmac-secret',
        'INTAKE_SMTP_PASSWORD': 'synthetic-intake-password',
        'DATABASE_URL': 'postgresql://synthetic-user:synthetic-db-password@db.invalid/faxbot?token=synthetic-query-secret',
    }
    view = project_admin_settings(snapshot(environment))
    assert view['security']['api_key'] == '***'
    assert view['phaxio']['api_key'] == view['phaxio']['api_secret'] == '***'
    assert view['sinch']['api_key'] == view['sinch']['api_secret'] == '***'
    assert view['documo']['api_key'] == '***'
    assert view['humblefax']['access_key'] == view['humblefax']['secret_key'] == '***'
    assert view['signalwire']['api_token'] == view['signalwire']['webhook_signing_key'] == '***'
    assert view['sip']['ami_password'] == view['fs']['esl_password'] == '***'
    assert view['inbound']['sip']['asterisk_secret'] == '***'
    assert view['inbound']['sinch']['basic_pass'] == view['inbound']['sinch']['hmac_secret'] == '***'
    assert view['intake']['smtp_password'] == '***'
    assert view['database']['url'] == '***'
    serialized = json.dumps(view)
    for secret in ('synthetic-admin-key', 'synthetic-phaxio-secret', 'synthetic-sinch-key', 'synthetic-sinch-secret',
                   'synthetic-documo-key', 'synthetic-humblefax-access', 'synthetic-humblefax-secret',
                   'synthetic-signalwire-token', 'synthetic-webhook-key',
                   'synthetic-ami-password', 'synthetic-esl-password', 'synthetic-inbound-secret',
                   'synthetic-basic-password', 'synthetic-hmac-secret', 'synthetic-db-password', 'synthetic-query-secret',
                   'synthetic-intake-password'):
        assert secret not in serialized

    empty = project_admin_settings(snapshot({key: '' for key in environment}))
    assert empty['security']['api_key'] == ''
    assert empty['phaxio']['api_key'] == empty['phaxio']['api_secret'] == ''
    assert empty['sinch']['api_key'] == empty['sinch']['api_secret'] == ''
    assert empty['documo']['api_key'] == empty['signalwire']['api_token'] == ''
    assert empty['humblefax']['access_key'] == empty['humblefax']['secret_key'] == ''
    assert empty['signalwire']['webhook_signing_key'] == ''
    assert empty['sip']['ami_password'] == empty['fs']['esl_password'] == ''
    assert empty['inbound']['sip']['asterisk_secret'] == ''
    assert empty['inbound']['sinch']['basic_pass'] == empty['inbound']['sinch']['hmac_secret'] == ''
    assert empty['intake']['smtp_password'] == ''
    assert empty['database']['url'] == ''
    assert empty['phaxio']['configured'] is False
    assert empty['documo']['configured'] is False
    assert empty['humblefax']['configured'] is False
    assert empty['signalwire']['configured'] is False


# Every masked value in the editor view; each is a credential or the database URL.
MASKED_PATHS = {
    'security.api_key', 'phaxio.api_key', 'phaxio.api_secret', 'phaxio.callback_token', 'sinch.api_key',
    'sinch.api_secret', 'documo.api_key', 'humblefax.access_key', 'humblefax.secret_key', 'signalwire.api_token',
    'signalwire.webhook_signing_key', 'sip.ami_password', 'sip.trunk.password', 'sip.telnyx_api_key', 'fs.esl_password',
    'inbound.sip.asterisk_secret', 'inbound.sinch.basic_pass', 'inbound.sinch.hmac_secret', 'intake.smtp_password',
    'database.url',
}


def _masked(view, prefix=''):
    for key, value in view.items():
        path = prefix + key
        if isinstance(value, dict):
            yield from _masked(value, path + '.')
        elif value == '***':
            yield path


def test_only_credentials_are_masked_and_fax_numbers_show_as_stored():
    """The station ID showed as *** in the Setup Wizard; numbers are not secrets."""
    from api.app.config_views import project_admin_settings
    from api.app.config_values import ConfigurationValues

    environment = {}
    for name, field in ConfigurationValues.model_fields.items():
        alias = field.validation_alias
        alias = alias if isinstance(alias, str) else alias.choices[0]
        if (field.json_schema_extra or {}).get('secret') or name == 'database_url':
            environment[alias] = 'sqlite:////faxdata/faxbot.db' if name == 'database_url' else 'synthetic-secret-value'
    environment.update({'FAX_LOCAL_STATION_ID': '+13035550100', 'SIGNALWIRE_FAX_FROM_E164': '+13035550101',
                        'SIGNALWIRE_SMS_FROM_E164': '+13035550102'})
    view = project_admin_settings(snapshot(environment))
    assert set(_masked(view)) == MASKED_PATHS
    assert view['sip']['station_id'] == '+13035550100'
    assert (view['signalwire']['from_fax'], view['signalwire']['from_sms']) == ('+13035550101', '+13035550102')
    assert 'synthetic-secret-value' not in json.dumps(view)


@pytest.mark.parametrize(('url', 'scheme', 'persistent'), [
    ('postgresql+psycopg://user:password@db.invalid/faxbot?sslpassword=synthetic-secret', 'postgresql', False),
    ('postgres://user:password@db.invalid/faxbot#synthetic-secret', 'postgresql', False),
    ('sqlite:////faxdata/faxbot.db?secret=synthetic-secret', 'sqlite', True),
    ('sqlite+aiosqlite:///./synthetic-private.db', 'sqlite', False),
    ('synthetic-private-scheme://synthetic-private-target', 'unknown', False),
    ('//synthetic-private-target?token=synthetic-private-secret', 'unknown', False),
    ('', '', False),
])
def test_database_display_never_contains_credentials_query_path_or_untrusted_scheme(url, scheme, persistent):
    from api.app.config_views import project_admin_settings

    assert project_admin_settings(snapshot({'DATABASE_URL': url}))['database'] == {
        'url': '***' if url else '', 'scheme': scheme, 'persistent': persistent,
        'editable': False, 'maintenance_required': True,
    }


def test_editor_has_omitted_provider_and_resource_settings_and_preserves_false_zero_empty():
    from api.app.config_views import project_admin_settings

    view = project_admin_settings(snapshot({
        'SINCH_BASE_URL': 'https://sinch.example.invalid', 'SINCH_PROJECT_ID': 'project',
        'SIGNALWIRE_STATUS_CALLBACK_URL': 'https://fax.example.invalid/callback',
        'SIGNALWIRE_STATUS_POLL_SECONDS': '0', 'FREESWITCH_T38_ENABLE': 'false',
        'AUDIT_LOG_ENABLED': 'false', 'AUDIT_LOG_FORMAT': 'text', 'AUDIT_LOG_FILE': '',
        'AUDIT_LOG_SYSLOG': 'false', 'AUDIT_LOG_SYSLOG_ADDRESS': 'localhost:514',
        'ENABLE_PERSISTED_SETTINGS': 'false', 'PERSISTED_ENV_PATH': '/faxdata/operator.env',
        'ENABLE_MCP_SSE': 'false', 'MCP_SSE_PATH': '/custom/sse', 'ENABLE_MCP_HTTP': 'false',
        'MCP_HTTP_PATH': '/custom/http', 'REQUIRE_MCP_OAUTH': 'false', 'OAUTH_ISSUER': '',
        'OAUTH_AUDIENCE': 'audience', 'OAUTH_JWKS_URL': 'https://issuer.example.invalid/jwks',
        'FEATURE_V3_PLUGINS': 'false', 'FEATURE_PLUGIN_INSTALL': 'false', 'INBOUND_ENABLED': 'false',
        'INBOUND_RETENTION_DAYS': '0', 'INBOUND_LIST_RPM': '0', 'INBOUND_GET_RPM': '0',
        'MAX_REQUESTS_PER_MINUTE': '0', 'ARTIFACT_TTL_DAYS': '0',
        'S3_BUCKET': 'complete-bucket', 'S3_PREFIX': '', 'S3_REGION': 'us-east-1',
        'S3_ENDPOINT_URL': 'https://storage.example.invalid', 'S3_KMS_KEY_ID': 'kms-key-identifier',
    }))
    assert view['sinch']['base_url'] == 'https://sinch.example.invalid'
    assert view['signalwire']['callback_url'] == 'https://fax.example.invalid/callback'
    assert view['signalwire']['status_poll_seconds'] == 0
    assert view['fs']['t38_enable'] is False
    assert view['audit'] == {'enabled': False, 'format': 'text', 'file': '', 'syslog': False, 'syslog_address': 'localhost:514'}
    assert view['persisted'] == {'enabled': False, 'path': '/faxdata/operator.env'}
    assert view['mcp'] == {
        'sse_enabled': False, 'sse_path': '/custom/sse', 'http_enabled': False, 'http_path': '/custom/http',
        'require_oauth': False, 'oauth': {'issuer': '', 'audience': 'audience', 'jwks_url': 'https://issuer.example.invalid/jwks'},
    }
    assert view['features'] == {'v3_plugins': False, 'fax_disabled': False, 'inbound_enabled': False, 'plugin_install': False}
    assert view['storage'] == {
        'backend': 'local', 's3_bucket': 'complete-bucket', 's3_prefix': '', 's3_region': 'us-east-1',
        's3_endpoint_url': 'https://storage.example.invalid', 's3_kms_key_id': 'kms-key-identifier', 's3_kms_enabled': True,
    }
    assert view['inbound']['enabled'] is False
    assert view['inbound']['retention_days'] == 0
    assert view['limits']['rate_limit_rpm'] == view['limits']['inbound_list_rpm'] == view['limits']['inbound_get_rpm'] == 0
    assert view['limits']['artifact_ttl_days'] == 0


def test_editor_projects_delivery_routes_intake_email_and_direct_delivery_settings():
    from api.app.config_views import project_admin_settings

    view = project_admin_settings(snapshot({
        'FAX_OUTBOUND_ROUTES': 'sip, phaxio', 'FAX_ROUTE_MIN_SUCCESS_PERCENT': '0',
        'INTAKE_EMAIL_ENABLED': 'true', 'INTAKE_SMTP_HOST': 'smtp.example.invalid', 'INTAKE_SMTP_PORT': '465',
        'INTAKE_SMTP_SECURITY': 'tls', 'INTAKE_SMTP_USERNAME': 'fax', 'INTAKE_EMAIL_FROM': 'fax@example.invalid',
        'INTAKE_EMAIL_TO': 'desk@example.invalid', 'INTAKE_EMAIL_SUBJECT': 'Fax for {to_number}',
        'DIRECT_DELIVERY_ENABLED': 'false', 'DIRECT_ORGANIZATION': 'County Clinic', 'DIRECT_FAX_NUMBER': '+12025550123',
    }))
    # The raw list is kept as written so an editor can show and save it unchanged.
    assert view['routing'] == {'outbound_routes': 'sip, phaxio', 'min_success_percent': 0}
    assert view['intake'] == {
        'email_enabled': True, 'smtp_host': 'smtp.example.invalid', 'smtp_port': 465, 'smtp_security': 'tls',
        'smtp_username': 'fax', 'smtp_password': '', 'email_from': 'fax@example.invalid',
        'email_to': 'desk@example.invalid', 'email_subject': 'Fax for {to_number}',
    }
    assert view['direct'] == {'enabled': False, 'organization': 'County Clinic', 'fax_number': '+12025550123',
                              'allow_private_peers': False}

    defaults = project_admin_settings(snapshot())
    assert defaults['routing'] == {'outbound_routes': '', 'min_success_percent': 80}
    assert defaults['intake']['email_enabled'] is False and defaults['intake']['smtp_port'] == 587
    assert defaults['direct'] == {'enabled': False, 'organization': '', 'fax_number': '', 'allow_private_peers': False}
