// A complete synthetic /admin/settings reply for console tests.
type Json = Record<string, any>;

export function settingsFixture(overrides: (data: Json) => void = () => undefined): Json {
  const data: Json = {
    backend: { type: 'phaxio', disabled: false },
    hybrid: { outbound_backend: 'phaxio', inbound_backend: 'phaxio', outbound_override: '', inbound_override: '' },
    phaxio: { api_key: '***', api_secret: '***', callback_token: '', callback_url: '', verify_signature: true, configured: true },
    documo: { api_key: '', base_url: 'https://api.documo.com', sandbox: false, configured: false },
    humblefax: { access_key: '***', secret_key: '***', from_number: '', configured: true },
    efax: { app_id: '', api_key: '', user_id: '', caller_id: '', csid: '', poll_seconds: 60, delete_after_download: false,
      webhook_secret: '', webhook_secret_set: false, configured: false },
    sinch: { project_id: '', base_url: '', api_key: '', api_secret: '', configured: false },
    signalwire: { space_url: '', project_id: '', api_token: '', from_fax: '', from_sms: '', callback_url: '',
      webhook_signing_key: '', status_poll_seconds: 0, configured: false },
    fs: { esl_host: '127.0.0.1', esl_port: 8021, esl_password: '***', gateway_name: 'gw', caller_id_number: '', t38_enable: true },
    sip: { ami_host: 'asterisk', ami_port: 5038, ami_username: 'api', ami_password: '***', ami_password_is_default: false,
      station_id: '+12025550100', configured: true },
    security: { api_key: '', require_api_key: false, enforce_https: true, audit_enabled: false, public_api_url: 'https://fax.example' },
    features: { v3_plugins: false, fax_disabled: false, inbound_enabled: false, plugin_install: false },
    storage: { backend: 'local', s3_bucket: '', s3_prefix: '', s3_region: '', s3_endpoint_url: '', s3_kms_key_id: '', s3_kms_enabled: false },
    routing: { outbound_routes: '', min_success_percent: 80 },
    intake: { email_enabled: false, smtp_host: '', smtp_port: 587, smtp_security: 'starttls', smtp_username: '',
      smtp_password: '', email_from: '', email_to: '', email_subject: 'Fax from {from_number}' },
    direct: { enabled: false, organization: '', fax_number: '' },
    inbound: { enabled: false, retention_days: 30, token_ttl_minutes: 60, sip: { asterisk_secret: '', configured: false },
      phaxio: { verify_signature: true }, sinch: { basic_auth_configured: false, hmac_configured: false } },
    limits: { max_file_size_mb: 10, pdf_token_ttl_minutes: 60, rate_limit_rpm: 0, inbound_list_rpm: 30, inbound_get_rpm: 60,
      artifact_ttl_days: 0, cleanup_interval_minutes: 1440 },
    _meta: { active_revision_id: 'rev-a', desired_revision_id: 'rev-a', generation: 4, apply_state: 'applied', pending_fields: [] },
  };
  overrides(data);
  return data;
}

// The provider fields /admin/settings reports for a sending/receiving pair.
export function withDirections(data: Json, sending: string, receiving: string) {
  data.backend.type = sending;
  const inbound = receiving && receiving !== sending ? receiving : '';
  data.hybrid = { outbound_backend: sending, inbound_backend: inbound || sending, outbound_override: '', inbound_override: inbound };
  data.inbound.enabled = !!receiving;
}

export function receipt(revision: string, pendingRestart = false) {
  return { ok: true, changed: true, _meta: { active_revision_id: pendingRestart ? 'rev-a' : revision,
    desired_revision_id: revision, generation: 5, apply_state: pendingRestart ? 'pending_restart' : 'applied',
    restart_recommended: pendingRestart } };
}
