"""Immutable typed configuration values, independent of process environment.

Loading, persistence and activation belong to their owning modules. Environment
aliases live on these fields so callers do not maintain competing field maps.
"""
from collections.abc import Mapping
import re

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, PrivateAttr, ValidationError, field_validator

from .config_paths import bundled_config_dir


class ConfigurationValueError(ValueError):
    """Configuration failed validation; issues never contain submitted values."""

    def __init__(self, issues: list[dict[str, str]]):
        self.issues = tuple(issues)
        fields = ", ".join(item["field"] for item in issues)
        super().__init__("Invalid configuration fields: " + fields)


class ConfigurationValues(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True, validate_default=True)

    fax_data_dir: str = Field('./faxdata', validation_alias='FAX_DATA_DIR')
    max_file_size_mb: int = Field(10, validation_alias='MAX_FILE_SIZE_MB', ge=1)
    fax_disabled: bool = Field(False, validation_alias='FAX_DISABLED')
    api_key: str = Field('', validation_alias='API_KEY', repr=False, json_schema_extra={'secret': True})
    require_api_key: bool = Field(False, validation_alias='REQUIRE_API_KEY')
    fax_backend: str = Field('phaxio', validation_alias='FAX_BACKEND', json_schema_extra={'patch_name': 'backend'})
    outbound_backend: str = Field('', validation_alias='FAX_OUTBOUND_BACKEND')
    inbound_backend: str = Field('', validation_alias='FAX_INBOUND_BACKEND')
    ami_host: str = Field('asterisk', validation_alias='ASTERISK_AMI_HOST')
    ami_port: int = Field(5038, validation_alias='ASTERISK_AMI_PORT', ge=1, le=65535)
    ami_username: str = Field('api', validation_alias='ASTERISK_AMI_USERNAME')
    ami_password: str = Field('changeme', validation_alias='ASTERISK_AMI_PASSWORD', repr=False, json_schema_extra={'secret': True})
    fs_esl_host: str = Field('127.0.0.1', validation_alias='FREESWITCH_ESL_HOST')
    fs_esl_port: int = Field(8021, validation_alias='FREESWITCH_ESL_PORT', ge=1, le=65535)
    fs_esl_password: str = Field('ClueCon', validation_alias='FREESWITCH_ESL_PASSWORD', repr=False, json_schema_extra={'secret': True})
    fs_gateway_name: str = Field('gw_signalwire', validation_alias='FREESWITCH_GATEWAY_NAME')
    fs_caller_id_number: str = Field('3035551234', validation_alias='FREESWITCH_CALLER_ID_NUMBER')
    fs_t38_enable: bool = Field(True, validation_alias='FREESWITCH_T38_ENABLE')
    phaxio_api_key: str = Field('', validation_alias='PHAXIO_API_KEY', repr=False, json_schema_extra={'secret': True})
    phaxio_api_secret: str = Field('', validation_alias='PHAXIO_API_SECRET', repr=False, json_schema_extra={'secret': True})
    phaxio_callback_token: str = Field('', validation_alias='PHAXIO_CALLBACK_TOKEN', repr=False, json_schema_extra={'secret': True})
    phaxio_status_callback_url: str = Field('', validation_alias=AliasChoices('PHAXIO_STATUS_CALLBACK_URL', 'PHAXIO_CALLBACK_URL'))
    phaxio_verify_signature: bool = Field(True, validation_alias='PHAXIO_VERIFY_SIGNATURE')
    public_api_url: str = Field('http://localhost:8080', validation_alias='PUBLIC_API_URL')
    sinch_base_url: str = Field('', validation_alias='SINCH_BASE_URL')
    sinch_project_id: str = Field('', validation_alias='SINCH_PROJECT_ID')
    sinch_api_key: str = Field('', validation_alias=AliasChoices('SINCH_API_KEY', 'PHAXIO_API_KEY'), repr=False, json_schema_extra={'secret': True})
    sinch_api_secret: str = Field('', validation_alias=AliasChoices('SINCH_API_SECRET', 'PHAXIO_API_SECRET'), repr=False, json_schema_extra={'secret': True})
    signalwire_space_url: str = Field('', validation_alias='SIGNALWIRE_SPACE_URL')
    signalwire_project_id: str = Field('', validation_alias='SIGNALWIRE_PROJECT_ID')
    signalwire_api_token: str = Field('', validation_alias='SIGNALWIRE_API_TOKEN', repr=False, json_schema_extra={'secret': True})
    signalwire_fax_from_e164: str = Field('', validation_alias='SIGNALWIRE_FAX_FROM_E164')
    signalwire_sms_from_e164: str = Field('', validation_alias='SIGNALWIRE_SMS_FROM_E164')
    signalwire_status_callback_url: str = Field('', validation_alias=AliasChoices('SIGNALWIRE_STATUS_CALLBACK_URL', 'SIGNALWIRE_CALLBACK_URL'))
    signalwire_webhook_signing_key: str = Field('', validation_alias='SIGNALWIRE_WEBHOOK_SIGNING_KEY', repr=False, json_schema_extra={'secret': True})
    signalwire_status_poll_seconds: int = Field(0, validation_alias='SIGNALWIRE_STATUS_POLL_SECONDS', ge=0)
    documo_api_key: str = Field('', validation_alias='DOCUMO_API_KEY', repr=False, json_schema_extra={'secret': True})
    documo_base_url: str = Field('https://api.documo.com', validation_alias='DOCUMO_BASE_URL')
    documo_use_sandbox: bool = Field(False, validation_alias='DOCUMO_SANDBOX')
    fax_header: str = Field('Faxbot', validation_alias='FAX_HEADER')
    fax_station_id: str = Field('+10000000000', validation_alias='FAX_LOCAL_STATION_ID')
    database_url: str = Field('sqlite:///./faxbot.db', validation_alias='DATABASE_URL', repr=False, json_schema_extra={'secret': True})
    pdf_token_ttl_minutes: int = Field(60, validation_alias='PDF_TOKEN_TTL_MINUTES', ge=1)
    enforce_public_https: bool = Field(True, validation_alias='ENFORCE_PUBLIC_HTTPS')
    artifact_ttl_days: int = Field(0, validation_alias='ARTIFACT_TTL_DAYS', ge=0)
    cleanup_interval_minutes: int = Field(1440, validation_alias='CLEANUP_INTERVAL_MINUTES', ge=1)
    max_requests_per_minute: int = Field(0, validation_alias='MAX_REQUESTS_PER_MINUTE', ge=0)
    audit_log_enabled: bool = Field(False, validation_alias='AUDIT_LOG_ENABLED')
    audit_log_format: str = Field('json', validation_alias='AUDIT_LOG_FORMAT')
    audit_log_file: str = Field('', validation_alias='AUDIT_LOG_FILE')
    audit_log_syslog: bool = Field(False, validation_alias='AUDIT_LOG_SYSLOG')
    audit_log_syslog_address: str = Field('/dev/log', validation_alias='AUDIT_LOG_SYSLOG_ADDRESS')
    inbound_enabled: bool = Field(False, validation_alias='INBOUND_ENABLED')
    inbound_retention_days: int = Field(30, validation_alias='INBOUND_RETENTION_DAYS', ge=0)
    inbound_token_ttl_minutes: int = Field(60, validation_alias='INBOUND_TOKEN_TTL_MINUTES', ge=1)
    asterisk_inbound_secret: str = Field('', validation_alias='ASTERISK_INBOUND_SECRET', repr=False, json_schema_extra={'secret': True})
    phaxio_inbound_verify_signature: bool = Field(True, validation_alias='PHAXIO_INBOUND_VERIFY_SIGNATURE')
    sinch_inbound_verify_signature: bool = Field(True, validation_alias='SINCH_INBOUND_VERIFY_SIGNATURE')
    sinch_inbound_basic_user: str = Field('', validation_alias='SINCH_INBOUND_BASIC_USER')
    sinch_inbound_basic_pass: str = Field('', validation_alias='SINCH_INBOUND_BASIC_PASS', repr=False, json_schema_extra={'secret': True})
    sinch_inbound_hmac_secret: str = Field('', validation_alias='SINCH_INBOUND_HMAC_SECRET', repr=False, json_schema_extra={'secret': True})
    storage_backend: str = Field('local', validation_alias='STORAGE_BACKEND')
    s3_bucket: str = Field('', validation_alias='S3_BUCKET')
    s3_prefix: str = Field('inbound/', validation_alias='S3_PREFIX')
    s3_region: str = Field('', validation_alias='S3_REGION')
    s3_endpoint_url: str = Field('', validation_alias='S3_ENDPOINT_URL')
    s3_kms_key_id: str = Field('', validation_alias='S3_KMS_KEY_ID')
    inbound_list_rpm: int = Field(30, validation_alias='INBOUND_LIST_RPM', ge=0)
    inbound_get_rpm: int = Field(60, validation_alias='INBOUND_GET_RPM', ge=0)
    admin_allow_restart: bool = Field(False, validation_alias='ADMIN_ALLOW_RESTART')
    enable_mcp_sse: bool = Field(False, validation_alias='ENABLE_MCP_SSE')
    mcp_sse_path: str = Field('/mcp/sse', validation_alias='MCP_SSE_PATH')
    require_mcp_oauth: bool = Field(False, validation_alias='REQUIRE_MCP_OAUTH')
    oauth_issuer: str = Field('', validation_alias='OAUTH_ISSUER')
    oauth_audience: str = Field('', validation_alias='OAUTH_AUDIENCE')
    oauth_jwks_url: str = Field('', validation_alias='OAUTH_JWKS_URL')
    enable_mcp_http: bool = Field(False, validation_alias='ENABLE_MCP_HTTP')
    mcp_http_path: str = Field('/mcp/http', validation_alias='MCP_HTTP_PATH')
    feature_v3_plugins: bool = Field(False, validation_alias='FEATURE_V3_PLUGINS')
    faxbot_config_path: str = Field(default_factory=lambda: str(bundled_config_dir() / 'faxbot.config.json'), validation_alias='FAXBOT_CONFIG_PATH')
    feature_plugin_install: bool = Field(False, validation_alias='FEATURE_PLUGIN_INSTALL')

    enable_persisted_settings: bool = Field(False, validation_alias='ENABLE_PERSISTED_SETTINGS')
    persisted_env_path: str = Field('/faxdata/faxbot.env', validation_alias='PERSISTED_ENV_PATH')
    providers_dir: str = Field(default_factory=lambda: str(bundled_config_dir() / 'providers'), validation_alias='FAXBOT_PROVIDERS_DIR')
    plugin_registry_path: str = Field(default_factory=lambda: str(bundled_config_dir() / 'plugin_registry.json'), validation_alias='PLUGIN_REGISTRY_PATH')

    _explicit_keys: frozenset[str] = PrivateAttr(default_factory=frozenset)

    @field_validator("fax_backend", "outbound_backend", "inbound_backend", "storage_backend", mode="before")
    @classmethod
    def normalize_selector(cls, value):
        return value.strip().lower() if isinstance(value, str) else value

    @classmethod
    def environment_keys(cls) -> frozenset[str]:
        keys = set()
        for field in cls.model_fields.values():
            alias = field.validation_alias
            keys.update(alias.choices if isinstance(alias, AliasChoices) else [alias])
        return frozenset(keys)

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> "ConfigurationValues":
        # Resolve aliases once per field. Leaving an unused legacy alias in
        # the Pydantic input would reject valid canonical+legacy coexistence.
        candidate = {}
        for field in cls.model_fields.values():
            alias = field.validation_alias
            choices = alias.choices if isinstance(alias, AliasChoices) else [alias]
            for key in choices:
                if key in environment:
                    candidate[choices[0]] = environment[key]
                    break
        try:
            values = cls.model_validate(candidate)
        except ValidationError as error:
            issues = [{"field": str(issue["loc"][0]), "reason": issue["type"]}
                      for issue in error.errors(include_input=False, include_context=False, include_url=False)]
            raise ConfigurationValueError(issues) from None
        values._explicit_keys = frozenset(environment.keys()) & cls.environment_keys()
        return values

    def to_environment(self, *, redact_secrets: bool = False) -> dict[str, str]:
        """Complete literal values; callers choose private or redacted output.

        Preserve absent directional overrides and inherited Sinch credentials.
        Materializing those fallbacks would change future edits after a reload.
        """
        result = {}
        for name, field in type(self).model_fields.items():
            alias = field.validation_alias
            key = alias.choices[0] if isinstance(alias, AliasChoices) else alias
            value = getattr(self, name)
            if name in {"outbound_backend", "inbound_backend"} and not value:
                continue
            if name in {"sinch_api_key", "sinch_api_secret"} and key not in self._explicit_keys:
                continue
            if redact_secrets and (field.json_schema_extra or {}).get("secret"):
                result[key] = "***" if value else ""
            else:
                result[key] = ("true" if value else "false") if isinstance(value, bool) else str(value)
        return result

    def with_patch(self, changes: Mapping[str, object]) -> "ConfigurationValues":
        """Validate a complete candidate without changing this frame or globals."""
        fields = {}
        for name, field in type(self).model_fields.items():
            metadata = field.json_schema_extra or {}
            fields[metadata.get("patch_name", name)] = field
        environment = self.to_environment()
        for name, value in changes.items():
            field = fields.get(name)
            if field is None:
                raise ConfigurationValueError([{"field": "<unknown>", "reason": "unknown_field"}])
            if value is None:
                continue
            alias = field.validation_alias
            key = alias.choices[0] if isinstance(alias, AliasChoices) else alias
            if (field.json_schema_extra or {}).get("secret") and isinstance(value, str) and re.fullmatch(r"\*+[\s\S]{0,4}", value):
                raise ConfigurationValueError([{"field": key, "reason": "masked_secret"}])
            if not isinstance(value, (str, int, bool)):
                raise ConfigurationValueError([{"field": key, "reason": "invalid_type"}])
            environment[key] = ("true" if value else "false") if isinstance(value, bool) else str(value)
        return type(self).from_environment(environment)

    def validate_provider_selection(self, registry: Mapping[str, object]) -> None:
        """Require explicit selections in the caller's validated provider registry."""
        known = set(registry) - {"_schema"}
        issues = []
        for key, selected in (("FAX_BACKEND", self.fax_backend),
                              ("FAX_OUTBOUND_BACKEND", self.effective_outbound),
                              ("FAX_INBOUND_BACKEND", self.effective_inbound)):
            if selected not in known:
                issues.append({"field": key, "reason": "unknown_provider"})
        if issues:
            raise ConfigurationValueError(issues)

    @property
    def effective_outbound(self) -> str:
        return self.outbound_backend or self.fax_backend

    @property
    def effective_inbound(self) -> str:
        return self.inbound_backend or self.fax_backend
