"""Immutable typed configuration values, independent of process environment.

Loading, persistence and activation belong to their owning modules. Environment
aliases live on these fields so callers do not maintain competing field maps.
"""
from collections.abc import Mapping
import re
from urllib.parse import urlsplit

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, PrivateAttr, ValidationError, field_validator

from .config_paths import bundled_config_dir


# Sinch's basic auth counts only with both a user name and a password; a user name alone would let anyone in.
SINCH_PASSWORD_NEEDED = 'Enter the password Sinch sends as well; Faxbot does not accept the user name without it.'
# One plain sentence for a refused setting, where the field's name alone would not say what to do.
FIELD_SENTENCES = {"FAX_TIME_ZONE": "Choose a time zone from the list, such as America/Denver.",
                   "SINCH_INBOUND_BASIC_PASS": SINCH_PASSWORD_NEEDED}


class ConfigurationValueError(ValueError):
    """Configuration failed validation; issues never contain submitted values."""

    def __init__(self, issues: list[dict[str, str]]):
        self.issues = tuple(issues)
        fields = ", ".join(item["field"] for item in issues)
        names = {item["field"] for item in issues}
        if len(names) == 1 and next(iter(names)) in FIELD_SENTENCES:
            super().__init__(FIELD_SENTENCES[next(iter(names))])
        else:
            super().__init__("Invalid configuration fields: " + fields)


# Credentials read from the environment at every start (config_runtime). API_KEY keeps
# its first-start and owner-recovery rules; DATABASE_URL changes need a datastore transfer.
ENVIRONMENT_CREDENTIAL_EXCLUSIONS = frozenset({"api_key", "database_url"})
# Variables of settings an earlier release had and this one removed. A settings file that still
# names one is accepted and the value ignored, for one release, so the installation still starts.
# SINCH_INBOUND_VERIFY_SIGNATURE and SINCH_INBOUND_HMAC_SECRET: Sinch's Fax API (v3) signs no webhooks, so
# the first checked nothing and the second refused every real notification.
RETIRED_ENVIRONMENT_KEYS = frozenset({"PLUGIN_REGISTRY_PATH", "SINCH_INBOUND_VERIFY_SIGNATURE",
                                      "SINCH_INBOUND_HMAC_SECRET"})
ENVIRONMENT_MANAGED_REFUSAL = "This key is set in .env. Change it there, then run docker compose up -d."

# Fax numbers in settings are saved in E.164; national input uses the country.
_NUMBER_FIELDS = frozenset({"direct_fax_number", "sip_trunk_caller_id", "sip_trunk_dids",
                            "signalwire_fax_from_e164", "efax_caller_id"})

# Read straight from the environment before they became configuration values. A saved
# configuration from before then takes each variable once, at the next start (config_runtime).
PROMOTED_FROM_ENVIRONMENT = ("sip_public_address_check_minutes", "enable_s3_diagnostics",
                             "mobile_local_base", "docs_base_url", "time_zone")


def usable_zone(value):
    """An IANA time zone name from TZ, or '' when it is UTC, empty or not a zone Faxbot can use."""
    if not isinstance(value, str):
        return ""
    name = value.strip().lstrip(":")
    if not name or name.upper() in {"UTC", "ETC/UTC", "GMT", "ETC/GMT", "UCT", "ZULU", "UNIVERSAL"}:
        return ""
    return name if _zone_name(name) else ""


def _zone_name(name) -> bool:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    if not isinstance(name, str) or not 0 < len(name) <= 64 or name.startswith("/") or ".." in name:
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return True


# Placeholder numbers earlier releases saved as defaults. A saved configuration that still holds
# exactly one of them has it cleared once, at the next start (config_runtime); a typed value stays.
PLACEHOLDER_DEFAULTS = {"fax_station_id": "+10000000000", "fs_caller_id_number": "3035551234"}


def _web_address(value) -> bool:
    if not (type(value) is str and 0 < len(value) <= 2048
            and all(33 <= ord(character) < 127 for character in value)
            and not any(character in value for character in '\\<>"\'')):
        return False
    try:
        parts = urlsplit(value)
        return (parts.scheme in {"http", "https"} and bool(parts.hostname)
                and parts.username is None and parts.password is None
                and not parts.query and not parts.fragment and parts.port != 0)
    except ValueError:
        return False


class ConfigurationValues(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True, validate_default=True)

    fax_data_dir: str = Field('./faxdata', validation_alias='FAX_DATA_DIR')
    max_file_size_mb: int = Field(10, validation_alias='MAX_FILE_SIZE_MB', ge=1)
    fax_disabled: bool = Field(False, validation_alias='FAX_DISABLED')
    api_key: str = Field('', validation_alias='API_KEY', repr=False, json_schema_extra={'secret': True})
    require_api_key: bool = Field(False, validation_alias='REQUIRE_API_KEY')
    # Empty means no fax provider is set up yet: a new installation sends and receives nothing until one is chosen.
    fax_backend: str = Field('', validation_alias='FAX_BACKEND', json_schema_extra={'patch_name': 'backend'})
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
    # The number your carrier gave you for FreeSWITCH calls; empty until entered (calls are refused without it).
    fs_caller_id_number: str = Field('', validation_alias='FREESWITCH_CALLER_ID_NUMBER')
    fs_t38_enable: bool = Field(True, validation_alias='FREESWITCH_T38_ENABLE')
    # SIP trunk for Faxbot's own fax engine (Asterisk). Empty preset keeps the
    # older SIP_USERNAME/SIP_SERVER container settings; empty host, port,
    # transport and codecs use the preset's documented values (see sip_trunk.py).
    sip_trunk_preset: str = Field('', validation_alias='SIP_TRUNK_PRESET',
                                  pattern=r'^(?:|telnyx|signalwire|sinch|anveo|flowroute|gamma|bt-one-voice|telstra-sip-connect|avaya-ipoffice|avaya-aura|custom)$')
    sip_trunk_auth: str = Field('registration', validation_alias='SIP_TRUNK_AUTH', pattern=r'^(?:registration|ip)$')
    sip_trunk_host: str = Field('', validation_alias='SIP_TRUNK_HOST',
                                pattern=r'^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*)?$')
    sip_trunk_port: int = Field(0, validation_alias='SIP_TRUNK_PORT', ge=0, le=65535)
    sip_trunk_transport: str = Field('', validation_alias='SIP_TRUNK_TRANSPORT', pattern=r'^(?:|udp|tcp|tls)$')
    sip_trunk_username: str = Field('', validation_alias='SIP_TRUNK_USERNAME', pattern=r'^[A-Za-z0-9_.+-]{0,128}$')
    sip_trunk_password: str = Field('', validation_alias=AliasChoices('SIP_TRUNK_PASSWORD', 'TELNYX_SIP_PASSWORD', 'TELNYX_PASS'),
                                    repr=False, json_schema_extra={'secret': True},
                                    pattern=r'^(?:[!-:<-\[\]-~](?:[ !-:<-\[\]-~]{0,126}[!-:<-\[\]-~])?)?$')
    sip_trunk_outbound_proxy: str = Field('', validation_alias='SIP_TRUNK_OUTBOUND_PROXY',
                                          pattern=r'^(?:[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?(?::[0-9]{1,5})?)?$')
    sip_trunk_caller_id: str = Field('', validation_alias='SIP_TRUNK_CALLER_ID', pattern=r'^(?:\+[1-9][0-9]{6,14})?$')
    sip_trunk_dids: str = Field('', validation_alias='SIP_TRUNK_DIDS',
                                pattern=r'^(?:\+[1-9][0-9]{6,14}(?:\s*,\s*\+[1-9][0-9]{6,14}){0,99})?$')
    sip_t38_enabled: bool = Field(True, validation_alias='SIP_T38_ENABLED')
    # Fax settings, as other fax servers offer them (both of Faxbot's fax engines; see sip_trunk.fax_options).
    # T.38 error correction: redundant copies of each packet (recommended), forward error correction, or none.
    sip_t38_error_correction: str = Field('redundancy', validation_alias='SIP_T38_ERROR_CORRECTION',
                                          pattern=r'^(?:redundancy|fec|none)$')
    sip_t38_max_datagram: int = Field(400, validation_alias='SIP_T38_MAX_DATAGRAM', ge=100, le=1400)
    # Highest fax speed in bit/s; audio calls never go above 9600, which survives a voice path better.
    sip_fax_max_rate: int = Field(14400, validation_alias='SIP_FAX_MAX_RATE')
    sip_fax_ecm: bool = Field(True, validation_alias='SIP_FAX_ECM')
    # The best compression Faxbot may agree with the other machine (mh < mr < mmr < jbig).
    sip_fax_compression: str = Field('jbig', validation_alias='SIP_FAX_COMPRESSION', pattern=r'^(?:mh|mr|mmr|jbig)$')
    sip_fax_fine: bool = Field(True, validation_alias='SIP_FAX_FINE')
    # SSL Fax engine (HylaFAX+): offered on every call; fax lines at once; the receiving listener's port,
    # used only when docker-compose.sslfax.yml publishes it.
    sip_sslfax_enabled: bool = Field(True, validation_alias='SIP_SSLFAX_ENABLED')
    sip_fax_lines: int = Field(2, validation_alias='SIP_FAX_LINES', ge=1, le=8)
    # Calls at once on the trunk (0: as many as the fax lines above) and new calls a second
    # (0: the carrier's published limit, or none). Faxes beyond either wait; they never fail for it.
    sip_trunk_max_calls: int = Field(0, validation_alias='SIP_TRUNK_MAX_CALLS', ge=0, le=200)
    sip_trunk_calls_per_second: int = Field(0, validation_alias='SIP_TRUNK_CALLS_PER_SECOND', ge=0, le=100)
    sip_sslfax_listener_port: int = Field(10443, validation_alias='SIP_SSLFAX_LISTENER_PORT', ge=1024, le=65535)
    # On by default: the RFC 6913 Accept-Contact preference only (never Require), which carriers may ignore.
    sip_fax_preference_header: bool = Field(True, validation_alias='SIP_FAX_PREFERENCE_HEADER')
    sip_trunk_codecs: str = Field('', validation_alias='SIP_TRUNK_CODECS', pattern=r'^(?:(?:ulaw|alaw)(?:,(?:ulaw|alaw))?)?$')
    # Phone systems only: how Faxbot writes the number it dials. Empty or e164 sends +<country><number>;
    # local sends the digits a phone at the installation dials, after the optional outside-line prefix.
    sip_trunk_dial_format: str = Field('', validation_alias='SIP_TRUNK_DIAL_FORMAT', pattern=r'^(?:|e164|local)$')
    sip_trunk_dial_prefix: str = Field('', validation_alias='SIP_TRUNK_DIAL_PREFIX', pattern=r'^[0-9]{0,4}$')
    # Public address the carrier should send signaling and media to when Asterisk is behind NAT.
    sip_external_address: str = Field('', validation_alias='SIP_EXTERNAL_ADDRESS',
                                      pattern=r'^(?:[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?)?$')
    # How often Faxbot checks its internet address again for the trunk, in minutes; 0 turns the check off.
    sip_public_address_check_minutes: int = Field(5, validation_alias='SIP_PUBLIC_ADDRESS_CHECK_MINUTES',
                                                  ge=0, le=1440)
    # Let Faxbot ask the router (PCP, NAT-PMP or UPnP) to open its published fax ports when the router changes
    # port numbers, so T.38 fax data can come back; Faxbot closes them when it stops.
    sip_router_ports: bool = Field(True, validation_alias='SIP_ROUTER_PORTS')
    # Telnyx API v2 key: Faxbot reads what each trunk call was charged and the trunk numbers' T.38 settings
    # (telnyx_t38.py). It changes one number's T.38 gateway only when a person selects Turn on T.38. Never used
    # to place calls.
    telnyx_api_key: str = Field('', validation_alias='TELNYX_API_KEY', repr=False, json_schema_extra={'secret': True},
                                pattern=r'^[!-~]{0,256}$')
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
    humblefax_access_key: str = Field('', validation_alias=AliasChoices('HUMBLEFAX_ACCESS_KEY', 'HUMBLEFAX_API_ACCESS_KEY'),
                                      repr=False, json_schema_extra={'secret': True})
    humblefax_secret_key: str = Field('', validation_alias=AliasChoices('HUMBLEFAX_SECRET_KEY', 'HUMBLEFAX_API_SECRET_KEY'),
                                      repr=False, json_schema_extra={'secret': True})
    humblefax_from_number: str = Field('', validation_alias='HUMBLEFAX_FROM_NUMBER', pattern=r'^(?:\+1[2-9][0-9]{9}|1?[2-9][0-9]{9})?$')
    # Receiving through HumbleFax: Faxbot asks HumbleFax for received faxes every humblefax_poll_seconds.
    # Off until turned on; HumbleFax as the receiving provider also turns it on (inbound/humblefax.py).
    humblefax_receive_enabled: bool = Field(False, validation_alias='HUMBLEFAX_RECEIVE_ENABLED')
    humblefax_poll_seconds: int = Field(60, validation_alias='HUMBLEFAX_POLL_SECONDS', ge=30, le=3600)
    # eFax Enterprise API (eFax Corporate): the app ID, API key and user ID from eFax's welcome email.
    efax_app_id: str = Field('', validation_alias='EFAX_APP_ID', repr=False, json_schema_extra={'secret': True},
                             pattern=r'^[!-9;-~]{0,256}$')
    efax_api_key: str = Field('', validation_alias='EFAX_API_KEY', repr=False, json_schema_extra={'secret': True},
                              pattern=r'^[!-~]{0,256}$')
    efax_user_id: str = Field('', validation_alias='EFAX_USER_ID', repr=False, json_schema_extra={'secret': True},
                              pattern=r'^[!-~]{0,256}$')
    # The eFax number recipients see (custom_CallerID) and the station name on each page (custom_CSID).
    efax_caller_id: str = Field('', validation_alias='EFAX_CALLER_ID', pattern=r'^(?:\+[1-9][0-9]{6,14})?$')
    efax_csid: str = Field('', validation_alias='EFAX_CSID', pattern=r'^[ -~]{0,20}$')
    # How often Faxbot asks eFax for received faxes, and whether it deletes each one from eFax once stored.
    efax_poll_seconds: int = Field(60, validation_alias='EFAX_POLL_SECONDS', ge=30, le=3600)
    efax_delete_after_download: bool = Field(False, validation_alias='EFAX_DELETE_AFTER_DOWNLOAD')
    # The HMAC secret given to eFax with a notification address; a signed notification makes Faxbot check eFax now.
    efax_webhook_secret: str = Field('', validation_alias='EFAX_WEBHOOK_SECRET', repr=False,
                                     json_schema_extra={'secret': True}, pattern=r'^[!-~]{0,256}$')
    fax_header: str = Field('Faxbot', validation_alias='FAX_HEADER')
    # The fax number printed for the receiving machine; empty means the trunk's caller ID, or none.
    fax_station_id: str = Field('', validation_alias='FAX_LOCAL_STATION_ID')
    # The number replies to your faxes reach (routing/reply_number.py): printed in each page's header line
    # and sent as the station ID. Empty: Faxbot uses your cheapest number that receives into a mailbox.
    fax_reply_number: str = Field('', validation_alias='FAX_REPLY_NUMBER', pattern=r'^(?:\+[1-9][0-9]{6,14})?$')
    # Mailboxes with a reply number of their own: "<mailbox ID>=<number>" pairs separated by semicolons.
    fax_reply_numbers: str = Field('', validation_alias='FAX_REPLY_NUMBERS', pattern=(
        r'^(?:[A-Za-z0-9_-]{1,40}=\+[1-9][0-9]{6,14}(?:;[A-Za-z0-9_-]{1,40}=\+[1-9][0-9]{6,14}){0,99})?$'))
    # Send-only numbers (routing/send_only.py): numbers shown as caller ID and station ID on faxes you send that
    # never receive faxes here, such as your main office number; comma-separated E.164.
    fax_send_only_numbers: str = Field('', validation_alias='FAX_SEND_ONLY_NUMBERS', pattern=(
        r'^(?:\+[1-9][0-9]{6,14}(?:,\+[1-9][0-9]{6,14}){0,49})?$'))
    # Installation country (ISO 3166 alpha-2, such as US or GB) for fax numbers
    # entered without a country code; every stored number is E.164.
    fax_default_country: str = Field('US', validation_alias='FAX_DEFAULT_COUNTRY')
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
    sinch_inbound_basic_user: str = Field('', validation_alias='SINCH_INBOUND_BASIC_USER')
    sinch_inbound_basic_pass: str = Field('', validation_alias='SINCH_INBOUND_BASIC_PASS', repr=False, json_schema_extra={'secret': True})
    # Where Sinch reaches Faxbot with received faxes when that is not PUBLIC_API_URL, such as a
    # tunnel that passes only /sinch-inbound. Empty uses PUBLIC_API_URL.
    sinch_webhook_base_url: str = Field('', validation_alias='SINCH_WEBHOOK_BASE_URL',
                                        pattern=r'^(?:|https?://[A-Za-z0-9.-]+(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~/-]*)?)$')
    storage_backend: str = Field('local', validation_alias='STORAGE_BACKEND')
    s3_bucket: str = Field('', validation_alias='S3_BUCKET')
    s3_prefix: str = Field('inbound/', validation_alias='S3_PREFIX')
    s3_region: str = Field('', validation_alias='S3_REGION')
    s3_endpoint_url: str = Field('', validation_alias='S3_ENDPOINT_URL')
    s3_kms_key_id: str = Field('', validation_alias='S3_KMS_KEY_ID')
    # Diagnostics also ask the S3 bucket whether Faxbot can reach it.
    enable_s3_diagnostics: bool = Field(False, validation_alias='ENABLE_S3_DIAGNOSTICS')
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

    # Delivery routes: extra outbound providers a fax may use, and the success
    # rate a route needs at a number before it stops being chosen first.
    outbound_routes: str = Field('', validation_alias='FAX_OUTBOUND_ROUTES', pattern=r'^[a-z0-9_.,\s-]*$')
    route_min_success_percent: int = Field(80, validation_alias='FAX_ROUTE_MIN_SUCCESS_PERCENT', ge=0, le=100)
    # Monthly normal-use budgets, allowances and billing days of flat and allowance plans, one entry a route
    # ("humblefax:pages=200,faxes=50,day=1"); empty uses Faxbot's cautious defaults (routing/plan_budget.py).
    plan_budgets: str = Field('', validation_alias='FAX_PLAN_BUDGETS', max_length=2000)
    # A fax to one of the installation's own receiving numbers becomes a received fax here, with no call.
    local_delivery_enabled: bool = Field(True, validation_alias='FAX_LOCAL_DELIVERY')
    # Lighten shaded areas and remove specks on documents you send (pages/friendly.py): 'where_it_saves' (the
    # default: only on calls billed by time and for machines without error correction), 'always' or 'never'.
    # The earlier on and off values read as always and never.
    fax_friendly_documents: str = Field('where_it_saves', validation_alias='FAX_FRIENDLY_DOCUMENTS',
                                        pattern=r'^(where_it_saves|always|never)$')
    # Default intake email connector; more connectors are managed in the console.
    intake_email_enabled: bool = Field(False, validation_alias='INTAKE_EMAIL_ENABLED')
    intake_smtp_host: str = Field('', validation_alias='INTAKE_SMTP_HOST')
    intake_smtp_port: int = Field(587, validation_alias='INTAKE_SMTP_PORT', ge=1, le=65535)
    intake_smtp_security: str = Field('starttls', validation_alias='INTAKE_SMTP_SECURITY', pattern=r'^(starttls|tls|none)$')
    intake_smtp_username: str = Field('', validation_alias='INTAKE_SMTP_USERNAME')
    intake_smtp_password: str = Field('', validation_alias='INTAKE_SMTP_PASSWORD', repr=False, json_schema_extra={'secret': True})
    intake_email_from: str = Field('', validation_alias='INTAKE_EMAIL_FROM')
    intake_email_to: str = Field('', validation_alias='INTAKE_EMAIL_TO')
    intake_email_subject: str = Field('Fax from {from_number}', validation_alias='INTAKE_EMAIL_SUBJECT')
    # Direct delivery between Faxbot installations.
    direct_delivery_enabled: bool = Field(False, validation_alias='DIRECT_DELIVERY_ENABLED')
    direct_organization: str = Field('', validation_alias='DIRECT_ORGANIZATION')
    direct_fax_number: str = Field('', validation_alias='DIRECT_FAX_NUMBER')
    # Partners on loopback, link-local or private addresses are refused unless this is on.
    direct_allow_private_peers: bool = Field(False, validation_alias='DIRECT_ALLOW_PRIVATE_PEERS')
    # Work queue: the team's operational target for acknowledging a received
    # document, in hours from when it arrived. 0 sets no target. Not a legal deadline.
    work_acknowledge_hours: int = Field(0, validation_alias='WORK_ACKNOWLEDGE_HOURS', ge=0, le=8760)
    # Case checklists: suggest documents that may match a missing item. Off unless turned on;
    # a suggestion is never sent unless a person adds it to the packet.
    case_suggestions_enabled: bool = Field(False, validation_alias='CASE_SUGGESTIONS')
    # The address paired phones use on the installation's own network; empty offers none.
    mobile_local_base: str = Field('', validation_alias='MOBILE_LOCAL_BASE')
    # Where the console's help links point.
    docs_base_url: str = Field('https://docs.faxbot.net/latest/', validation_alias='DOCS_BASE_URL')
    # The installation's time zone (IANA name) for times the server writes for people, such as the
    # received time in an email. At the first start the process's TZ is taken when it names a zone
    # other than UTC; empty means UTC.
    time_zone: str = Field('', validation_alias=AliasChoices('FAX_TIME_ZONE', 'TZ'))

    _explicit_keys: frozenset[str] = PrivateAttr(default_factory=frozenset)
    # The extra provider accounts of the configuration revision these values were read from (accounts.py):
    # a read-only view for code that has only the values. Revisions write accounts from their own record,
    # never from here.
    _provider_accounts: object = PrivateAttr(default=None)

    @field_validator("fax_backend", "outbound_backend", "inbound_backend", "storage_backend", mode="before")
    @classmethod
    def normalize_selector(cls, value):
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("plan_budgets")
    @classmethod
    def normalize_plan_budgets(cls, value):
        from .routing.plan_budget import normalize_budgets
        return normalize_budgets(value)

    @field_validator("fax_default_country", mode="before")
    @classmethod
    def normalize_country(cls, value):
        from .routing.numbers import SUPPORTED_COUNTRIES
        if isinstance(value, str):
            value = value.strip().upper()
            if value not in SUPPORTED_COUNTRIES:
                raise ValueError("unsupported country")
        return value

    @field_validator("fax_friendly_documents", mode="before")
    @classmethod
    def normalize_friendly_documents(cls, value):
        # On and off (a switch before the three choices) are always and never.
        if isinstance(value, bool):
            return 'always' if value else 'never'
        if isinstance(value, str):
            value = value.strip().lower()
            return {'true': 'always', 'on': 'always', 'yes': 'always', '1': 'always',
                    'false': 'never', 'off': 'never', 'no': 'never', '0': 'never'}.get(value, value)
        return value

    @field_validator("sip_fax_max_rate")
    @classmethod
    def require_fax_rate(cls, value):
        if value not in (14400, 9600, 7200, 4800):
            raise ValueError("fax speed must be 14400, 9600, 7200 or 4800")
        return value

    @field_validator("time_zone")
    @classmethod
    def require_time_zone(cls, value):
        if value != "" and not _zone_name(value):
            raise ValueError("unknown time zone")
        return value

    @field_validator("docs_base_url", "mobile_local_base")
    @classmethod
    def require_web_address(cls, value, info):
        # The console's help links and the address paired phones use are base addresses:
        # http or https with a host, no credentials, query or fragment (as access.context checks).
        if value == "" and info.field_name == "mobile_local_base":
            return value
        if not _web_address(value):
            raise ValueError("invalid web address")
        return value

    @classmethod
    def environment_keys(cls) -> frozenset[str]:
        keys = set()
        for field in cls.model_fields.values():
            alias = field.validation_alias
            keys.update(alias.choices if isinstance(alias, AliasChoices) else [alias])
        return frozenset(keys)

    @classmethod
    def accepted_environment_keys(cls) -> frozenset[str]:
        """Variables a settings file may name: every setting's, and retired ones that are ignored."""
        return cls.environment_keys() | RETIRED_ENVIRONMENT_KEYS

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> "ConfigurationValues":
        # Resolve aliases once per field. Leaving an unused legacy alias in
        # the Pydantic input would reject valid canonical+legacy coexistence.
        candidate = {}
        for field in cls.model_fields.values():
            alias = field.validation_alias
            choices = alias.choices if isinstance(alias, AliasChoices) else [alias]
            for key in choices:
                if key == "TZ":
                    # The process's own TZ only suggests the installation's zone: never UTC, never refused.
                    if usable_zone(environment.get(key)):
                        candidate[choices[0]] = usable_zone(environment[key])
                        break
                    continue
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

    @classmethod
    def environment_credentials(cls, environment: Mapping[str, str]) -> dict[str, tuple[str, str]]:
        """Credentials the environment supplies, as {field: (variable, value)}.

        Every setting marked secret except API_KEY and DATABASE_URL. An empty
        variable supplies nothing. Sinch's optional Phaxio fallback is not an
        explicit Sinch credential.
        """
        result = {}
        for name, field in cls.model_fields.items():
            if name in ENVIRONMENT_CREDENTIAL_EXCLUSIONS or not (field.json_schema_extra or {}).get("secret"):
                continue
            alias = field.validation_alias
            choices = list(alias.choices if isinstance(alias, AliasChoices) else [alias])
            if name in {"sinch_api_key", "sinch_api_secret"}:
                choices = choices[:1]
            for key in choices:
                value = environment.get(key)
                if isinstance(value, str) and value != "":
                    result[name] = (key, value)
                    break
        return result

    @classmethod
    def environment_adoptions(cls, environment: Mapping[str, str],
                              saved: "ConfigurationValues") -> dict[str, tuple[str, str]]:
        """Promoted settings the environment supplies that the saved configuration predates.

        Returns {field: (variable, value)}. A configuration saved since a setting was
        promoted carries it, so its variable is never taken again: the console and
        the command line own the value from then on.
        """
        result = {}
        for name in PROMOTED_FROM_ENVIRONMENT:
            alias = cls.model_fields[name].validation_alias
            choices = alias.choices if isinstance(alias, AliasChoices) else [alias]
            if choices[0] in saved._explicit_keys:
                continue
            for key in choices:
                if key not in environment:
                    continue
                value = usable_zone(environment[key]) if key == "TZ" else environment[key]
                if key == "TZ" and not value:
                    continue  # the process's TZ only suggests a zone; UTC or an unknown one suggests none
                result[name] = (key, value)
                break
        return result

    def placeholder_clearings(self) -> dict[str, str]:
        """Settings that still hold an earlier release's placeholder number, as {field: ''}."""
        return {name: "" for name, placeholder in PLACEHOLDER_DEFAULTS.items() if getattr(self, name) == placeholder}

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
            if isinstance(value, str) and name in _NUMBER_FIELDS:
                value = self._saved_number(name, value, changes)
            environment[key] = ("true" if value else "false") if isinstance(value, bool) else str(value)
        values = type(self).from_environment(environment)
        values._provider_accounts = self._provider_accounts
        if ({"sinch_inbound_basic_user", "sinch_inbound_basic_pass"} & set(changes)
                and values.sinch_inbound_basic_user and not values.sinch_inbound_basic_pass):
            raise ConfigurationValueError([{"field": "SINCH_INBOUND_BASIC_PASS", "reason": "required_with_user"}])
        return values

    @property
    def provider_accounts(self) -> dict:
        """The extra provider accounts, {key: document}, as their revision stores them; {} when there are none."""
        document = self._provider_accounts
        return document.as_dict() if document is not None else {}

    def with_provider_accounts(self, document) -> "ConfigurationValues":
        """A copy of these values whose read-only accounts view is ``document`` (a ConfigurationDocument)."""
        values = self.model_copy()
        values._explicit_keys = self._explicit_keys
        values._provider_accounts = document if document is not None and document.as_dict() else None
        return values

    @property
    def sinch_inbound_basic_configured(self) -> bool:
        """Sinch's basic auth is in force only with both a user name and a password."""
        return bool(self.sinch_inbound_basic_user and self.sinch_inbound_basic_pass)

    @property
    def sinch_incoming_webhook_url(self) -> str:
        """The exact address to paste into Sinch's fax service as its Incoming webhook URL."""
        return (self.sinch_webhook_base_url or self.public_api_url).rstrip('/') + '/sinch-inbound'

    @property
    def sinch_incoming_webhook_login_url(self) -> str | None:
        """That address with the basic-auth user name in it and PASSWORD where the password goes; None without one.

        Sinch takes webhook credentials inside the address, as https://username:password@host
        (Fax API v3 reference, WebhookBasicAuth, read 2026-10-07).
        """
        from urllib.parse import quote
        if not self.sinch_inbound_basic_configured:
            return None
        scheme, _, rest = self.sinch_incoming_webhook_url.partition('://')
        return f"{scheme}://{quote(self.sinch_inbound_basic_user, safe='')}:PASSWORD@{rest}"

    def _saved_number(self, name, value, changes):
        """Save numbers entered nationally for the installation country in E.164.

        Unreadable input is kept as typed so the field's own validation refuses
        it with its usual message.
        """
        from .routing.numbers import SUPPORTED_COUNTRIES, stored_number
        country = changes.get("fax_default_country") or self.fax_default_country
        country = country.strip().upper() if isinstance(country, str) else ""
        if country not in SUPPORTED_COUNTRIES:
            return value
        if name == "sip_trunk_dids":
            parts = [part.strip() for part in value.split(",") if part.strip()]
            return ",".join(stored_number(part, country=country) for part in parts)
        return stored_number(value, country=country) if value.strip() else value

    def validate_provider_selection(self, registry: Mapping[str, object]) -> None:
        """Require explicit selections in the caller's validated provider registry.

        An empty selection means no provider is set up for that role yet.
        """
        known = set(registry) - {"_schema"}
        issues = []
        for key, selected in (("FAX_BACKEND", self.fax_backend),
                              ("FAX_OUTBOUND_BACKEND", self.effective_outbound),
                              ("FAX_INBOUND_BACKEND", self.effective_inbound)):
            if selected and selected not in known:
                issues.append({"field": key, "reason": "unknown_provider"})
        if issues:
            raise ConfigurationValueError(issues)

    @property
    def outbound_route_providers(self) -> tuple[str, ...]:
        """Extra outbound providers listed in FAX_OUTBOUND_ROUTES, in order, without the default one."""
        result = []
        for part in self.outbound_routes.split(','):
            identity = part.strip().lower()
            if identity and identity != self.effective_outbound and identity not in result:
                result.append(identity)
        return tuple(result)

    @property
    def effective_outbound(self) -> str:
        return self.outbound_backend or self.fax_backend

    @property
    def effective_inbound(self) -> str:
        return self.inbound_backend or self.fax_backend

    @property
    def sip_trunk_did_list(self) -> tuple[str, ...]:
        """Numbers the carrier routes to this trunk, in entered order, without repeats."""
        result = []
        for part in self.sip_trunk_dids.split(','):
            number = part.strip()
            if number and number not in result:
                result.append(number)
        return tuple(result)
