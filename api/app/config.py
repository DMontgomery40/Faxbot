import os
import json
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field


# Optional: load persisted settings from a file before constructing Settings
# Controlled by ENABLE_PERSISTED_SETTINGS=true and optional PERSISTED_ENV_PATH
def _load_persisted_env_if_enabled() -> None:
    try:
        enabled = os.getenv("ENABLE_PERSISTED_SETTINGS", "false").lower() in {"1", "true", "yes"}
        if not enabled:
            return
        path = os.getenv("PERSISTED_ENV_PATH", "/faxdata/faxbot.env")
        if not os.path.exists(path):
            return
        with open(path, "r") as f:
            for raw in f.readlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                # Strip optional surrounding quotes
                v = val.strip()
                if (v.startswith("\"") and v.endswith("\"")) or (v.startswith("'") and v.endswith("'")):
                    v = v[1:-1]
                # Persisted file overrides process env when enabled
                os.environ[key] = v
    except Exception:
        # Fail open: if persisted load fails, continue with process env
        pass


_load_persisted_env_if_enabled()


class Settings(BaseModel):
    # App
    fax_data_dir: str = Field(default_factory=lambda: os.getenv("FAX_DATA_DIR", "./faxdata"))
    max_file_size_mb: int = Field(default_factory=lambda: int(os.getenv("MAX_FILE_SIZE_MB", "10")))
    fax_disabled: bool = Field(default_factory=lambda: os.getenv("FAX_DISABLED", "false").lower() in {"1", "true", "yes"})
    api_key: str = Field(default_factory=lambda: os.getenv("API_KEY", ""))
    # Require API key on requests regardless of env API_KEY. Useful for HIPAA prod.
    require_api_key: bool = Field(default_factory=lambda: os.getenv("REQUIRE_API_KEY", "false").lower() in {"1", "true", "yes"})

    # Fax Backend Selection
    # Legacy single-backend env (fallback for both outbound/inbound when dual is unset)
    fax_backend: str = Field(default_factory=lambda: os.getenv("FAX_BACKEND", "phaxio").lower())  # default to cloud; require explicit 'sip' for telephony
    # Dual-backend (hybrid) envs — when set, override legacy for each direction
    outbound_backend: str = Field(default_factory=lambda: (os.getenv("FAX_OUTBOUND_BACKEND", "") or os.getenv("FAX_BACKEND", "phaxio")).lower())
    inbound_backend: str = Field(default_factory=lambda: (os.getenv("FAX_INBOUND_BACKEND", "") or os.getenv("FAX_BACKEND", "phaxio")).lower())

    # Asterisk AMI (for SIP backend)
    ami_host: str = Field(default_factory=lambda: os.getenv("ASTERISK_AMI_HOST", "asterisk"))
    ami_port: int = Field(default_factory=lambda: int(os.getenv("ASTERISK_AMI_PORT", "5038")))
    ami_username: str = Field(default_factory=lambda: os.getenv("ASTERISK_AMI_USERNAME", "api"))
    ami_password: str = Field(default_factory=lambda: os.getenv("ASTERISK_AMI_PASSWORD", "changeme"))

    # FreeSWITCH ESL (preview)
    fs_esl_host: str = Field(default_factory=lambda: os.getenv("FREESWITCH_ESL_HOST", "127.0.0.1"))
    fs_esl_port: int = Field(default_factory=lambda: int(os.getenv("FREESWITCH_ESL_PORT", "8021")))
    fs_esl_password: str = Field(default_factory=lambda: os.getenv("FREESWITCH_ESL_PASSWORD", "ClueCon"))
    fs_gateway_name: str = Field(default_factory=lambda: os.getenv("FREESWITCH_GATEWAY_NAME", "gw_signalwire"))
    fs_caller_id_number: str = Field(default_factory=lambda: os.getenv("FREESWITCH_CALLER_ID_NUMBER", "3035551234"))
    fs_t38_enable: bool = Field(default_factory=lambda: os.getenv("FREESWITCH_T38_ENABLE", "true").lower() in {"1","true","yes"})

    # Phaxio Configuration (for cloud backend)
    phaxio_api_key: str = Field(default_factory=lambda: os.getenv("PHAXIO_API_KEY", ""))
    phaxio_api_secret: str = Field(default_factory=lambda: os.getenv("PHAXIO_API_SECRET", ""))
    # Support both PHAXIO_STATUS_CALLBACK_URL and PHAXIO_CALLBACK_URL per AGENTS.md
    phaxio_status_callback_url: str = Field(
        default_factory=lambda: os.getenv("PHAXIO_STATUS_CALLBACK_URL", os.getenv("PHAXIO_CALLBACK_URL", ""))
    )
    # Verify Phaxio webhook signatures (HMAC-SHA256) — default on; allow explicit dev opt-out
    phaxio_verify_signature: bool = Field(default_factory=lambda: os.getenv("PHAXIO_VERIFY_SIGNATURE", "true").lower() in {"1", "true", "yes"})

    # Public API URL (needed for cloud backend to fetch PDFs, e.g., Phaxio)
    public_api_url: str = Field(default_factory=lambda: os.getenv("PUBLIC_API_URL", "http://localhost:8080"))

    # Sinch Fax (Phaxio by Sinch) — direct upload flow
    sinch_project_id: str = Field(default_factory=lambda: os.getenv("SINCH_PROJECT_ID", ""))
    sinch_api_key: str = Field(default_factory=lambda: os.getenv("SINCH_API_KEY", os.getenv("PHAXIO_API_KEY", "")))
    sinch_api_secret: str = Field(default_factory=lambda: os.getenv("SINCH_API_SECRET", os.getenv("PHAXIO_API_SECRET", "")))

    # SignalWire (Compatibility Fax API)
    signalwire_space_url: str = Field(default_factory=lambda: os.getenv("SIGNALWIRE_SPACE_URL", ""))
    signalwire_project_id: str = Field(default_factory=lambda: os.getenv("SIGNALWIRE_PROJECT_ID", ""))
    signalwire_api_token: str = Field(default_factory=lambda: os.getenv("SIGNALWIRE_API_TOKEN", ""))
    signalwire_fax_from_e164: str = Field(default_factory=lambda: os.getenv("SIGNALWIRE_FAX_FROM_E164", ""))
    signalwire_sms_from_e164: str = Field(default_factory=lambda: os.getenv("SIGNALWIRE_SMS_FROM_E164", ""))
    signalwire_status_callback_url: str = Field(default_factory=lambda: os.getenv("SIGNALWIRE_STATUS_CALLBACK_URL", os.getenv("SIGNALWIRE_CALLBACK_URL", "")))
    signalwire_webhook_signing_key: str = Field(default_factory=lambda: os.getenv("SIGNALWIRE_WEBHOOK_SIGNING_KEY", ""))
    signalwire_status_poll_seconds: int = Field(default_factory=lambda: int(os.getenv("SIGNALWIRE_STATUS_POLL_SECONDS", "0")))

    # Documo (mFax) — direct upload flow (preview)
    documo_api_key: str = Field(default_factory=lambda: os.getenv("DOCUMO_API_KEY", ""))
    documo_base_url: str = Field(default_factory=lambda: os.getenv("DOCUMO_BASE_URL", "https://api.documo.com"))
    documo_use_sandbox: bool = Field(default_factory=lambda: os.getenv("DOCUMO_SANDBOX", "false").lower() in {"1", "true", "yes"})

    # Fax presentation
    fax_header: str = Field(default_factory=lambda: os.getenv("FAX_HEADER", "Faxbot"))
    fax_station_id: str = Field(default_factory=lambda: os.getenv("FAX_LOCAL_STATION_ID", "+10000000000"))

    # DB
    database_url: str = Field(default_factory=lambda: os.getenv("DATABASE_URL", "sqlite:///./faxbot.db"))

    # Security
    pdf_token_ttl_minutes: int = Field(default_factory=lambda: int(os.getenv("PDF_TOKEN_TTL_MINUTES", "60")))
    enforce_public_https: bool = Field(default_factory=lambda: os.getenv("ENFORCE_PUBLIC_HTTPS", "true").lower() in {"1", "true", "yes"})

    # Retention / cleanup
    artifact_ttl_days: int = Field(default_factory=lambda: int(os.getenv("ARTIFACT_TTL_DAYS", "0")))  # 0=disabled
    cleanup_interval_minutes: int = Field(default_factory=lambda: int(os.getenv("CLEANUP_INTERVAL_MINUTES", "1440")))

    # Rate limiting (per key) — disabled by default; implemented in Phase 2
    max_requests_per_minute: int = Field(default_factory=lambda: int(os.getenv("MAX_REQUESTS_PER_MINUTE", "0")))

    # Audit logging
    audit_log_enabled: bool = Field(default_factory=lambda: os.getenv("AUDIT_LOG_ENABLED", "false").lower() in {"1", "true", "yes"})
    audit_log_format: str = Field(default_factory=lambda: os.getenv("AUDIT_LOG_FORMAT", "json"))
    audit_log_file: str = Field(default_factory=lambda: os.getenv("AUDIT_LOG_FILE", ""))
    audit_log_syslog: bool = Field(default_factory=lambda: os.getenv("AUDIT_LOG_SYSLOG", "false").lower() in {"1", "true", "yes"})
    audit_log_syslog_address: str = Field(default_factory=lambda: os.getenv("AUDIT_LOG_SYSLOG_ADDRESS", "/dev/log"))

    # Inbound receiving (Phase Receive)
    inbound_enabled: bool = Field(default_factory=lambda: os.getenv("INBOUND_ENABLED", "false").lower() in {"1", "true", "yes"})
    inbound_retention_days: int = Field(default_factory=lambda: int(os.getenv("INBOUND_RETENTION_DAYS", "30")))
    inbound_token_ttl_minutes: int = Field(default_factory=lambda: int(os.getenv("INBOUND_TOKEN_TTL_MINUTES", "60")))
    asterisk_inbound_secret: str = Field(default_factory=lambda: os.getenv("ASTERISK_INBOUND_SECRET", ""))
    phaxio_inbound_verify_signature: bool = Field(default_factory=lambda: os.getenv("PHAXIO_INBOUND_VERIFY_SIGNATURE", "true").lower() in {"1", "true", "yes"})
    sinch_inbound_verify_signature: bool = Field(default_factory=lambda: os.getenv("SINCH_INBOUND_VERIFY_SIGNATURE", "true").lower() in {"1", "true", "yes"})
    sinch_inbound_basic_user: str = Field(default_factory=lambda: os.getenv("SINCH_INBOUND_BASIC_USER", ""))
    sinch_inbound_basic_pass: str = Field(default_factory=lambda: os.getenv("SINCH_INBOUND_BASIC_PASS", ""))
    sinch_inbound_hmac_secret: str = Field(default_factory=lambda: os.getenv("SINCH_INBOUND_HMAC_SECRET", ""))

    # Storage backend for inbound artifacts
    storage_backend: str = Field(default_factory=lambda: os.getenv("STORAGE_BACKEND", "local"))  # local | s3
    s3_bucket: str = Field(default_factory=lambda: os.getenv("S3_BUCKET", ""))
    s3_prefix: str = Field(default_factory=lambda: os.getenv("S3_PREFIX", "inbound/"))
    s3_region: str = Field(default_factory=lambda: os.getenv("S3_REGION", ""))
    s3_endpoint_url: str = Field(default_factory=lambda: os.getenv("S3_ENDPOINT_URL", ""))  # allow S3-compatible (MinIO)
    s3_kms_key_id: str = Field(default_factory=lambda: os.getenv("S3_KMS_KEY_ID", ""))

    # Inbound rate limits (per key)
    inbound_list_rpm: int = Field(default_factory=lambda: int(os.getenv("INBOUND_LIST_RPM", "30")))
    inbound_get_rpm: int = Field(default_factory=lambda: int(os.getenv("INBOUND_GET_RPM", "60")))

    # Admin console options
    admin_allow_restart: bool = Field(default_factory=lambda: os.getenv("ADMIN_ALLOW_RESTART", "false").lower() in {"1","true","yes"})

    # MCP (embedded) settings
    enable_mcp_sse: bool = Field(default_factory=lambda: os.getenv("ENABLE_MCP_SSE", "false").lower() in {"1","true","yes"})
    mcp_sse_path: str = Field(default_factory=lambda: os.getenv("MCP_SSE_PATH", "/mcp/sse"))
    require_mcp_oauth: bool = Field(default_factory=lambda: os.getenv("REQUIRE_MCP_OAUTH", "false").lower() in {"1","true","yes"})
    oauth_issuer: str = Field(default_factory=lambda: os.getenv("OAUTH_ISSUER", ""))
    oauth_audience: str = Field(default_factory=lambda: os.getenv("OAUTH_AUDIENCE", ""))
    oauth_jwks_url: str = Field(default_factory=lambda: os.getenv("OAUTH_JWKS_URL", ""))
    # MCP HTTP transport
    enable_mcp_http: bool = Field(default_factory=lambda: os.getenv("ENABLE_MCP_HTTP", "false").lower() in {"1","true","yes"})
    mcp_http_path: str = Field(default_factory=lambda: os.getenv("MCP_HTTP_PATH", "/mcp/http"))

    # v3 Plugins (feature-gated)
    feature_v3_plugins: bool = Field(default_factory=lambda: os.getenv("FEATURE_V3_PLUGINS", "false").lower() in {"1","true","yes"})
    faxbot_config_path: str = Field(default_factory=lambda: os.getenv("FAXBOT_CONFIG_PATH", "config/faxbot.config.json"))
    feature_plugin_install: bool = Field(default_factory=lambda: os.getenv("FEATURE_PLUGIN_INSTALL", "false").lower() in {"1","true","yes"})


settings = Settings()


def reload_settings() -> None:
    """Reload settings from current environment into the existing instance.
    Keeps references stable across modules that imported `settings`.
    """
    new = Settings()
    for name in new.model_fields.keys():  # type: ignore[attr-defined]
        setattr(settings, name, getattr(new, name))
    # Rebuild traits cache on settings reload
    try:
        _refresh_traits_cache()
    except Exception:
        pass

# ===== Provider traits registry (declarative) =====
_TRAITS_CACHE: Dict[str, Any] = {"registry": {}, "loaded_mtime": 0.0, "schema_issues": {}}

# Canonical trait keys — schema contract
CANONICAL_TRAIT_KEYS: set[str] = {
    "requires_ghostscript",
    "requires_ami",
    "requires_tiff",
    "supports_inbound",
    "inbound_verification",
    "needs_storage",
    "outbound_status_only",
}


def _traits_file_path() -> str:
    # Project-relative config folder
    return os.path.join(os.getcwd(), "config", "provider_traits.json")


def _providers_dir() -> str:
    return os.path.join(os.getcwd(), "config", "providers")


def _read_json(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception:
        return None


def _load_base_traits() -> Dict[str, Dict[str, Any]]:
    base = _read_json(_traits_file_path())
    reg: Dict[str, Dict[str, Any]] = {}
    if not base:
        return reg
    # Accept either an object keyed by id, or a list of providers
    if isinstance(base, dict):
        for pid, obj in base.items():
            if isinstance(obj, dict):
                obj.setdefault("id", pid)
                reg[pid] = obj
    elif isinstance(base, list):
        for obj in base:
            if isinstance(obj, dict) and obj.get("id"):
                reg[str(obj["id"])]= obj
    return reg


def _merge(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(a or {})
    for k, v in (b or {}).items():
        if k == "traits" and isinstance(v, dict):
            tv = dict(out.get("traits") or {})
            # Filter unknown trait keys and record issues
            unknown = [kk for kk in v.keys() if kk not in CANONICAL_TRAIT_KEYS]
            if unknown:
                issues = _TRAITS_CACHE.get("schema_issues") or {}
                pid = (a.get("id") or b.get("id") or "unknown")
                issues.setdefault("unknown_trait_keys", {})[str(pid)] = sorted(set(unknown))
                _TRAITS_CACHE["schema_issues"] = issues
            # Merge only canonical keys
            for kk, vv in v.items():
                if kk in CANONICAL_TRAIT_KEYS:
                    tv[kk] = vv
            out["traits"] = tv
        else:
            out[k] = v
    return out


def _scan_manifest_traits() -> Dict[str, Dict[str, Any]]:
    results: Dict[str, Dict[str, Any]] = {}
    pdir = _providers_dir()
    if not os.path.isdir(pdir):
        return results
    for pid in os.listdir(pdir):
        mpath = os.path.join(pdir, pid, "manifest.json")
        if not os.path.exists(mpath):
            continue
        data = _read_json(mpath)
        if not isinstance(data, dict):
            continue
        # Traits are optional in manifests; use if present
        traits = data.get("traits") if isinstance(data.get("traits"), dict) else None
        kind = data.get("kind") if isinstance(data.get("kind"), str) else None
        obj: Dict[str, Any] = {"id": pid}
        if kind:
            obj["kind"] = kind
        if traits:
            obj["traits"] = traits
        if obj:
            results[pid] = obj
    return results


def _build_provider_registry() -> Dict[str, Dict[str, Any]]:
    reg = _load_base_traits()
    # Merge in manifests (override base)
    man = _scan_manifest_traits()
    for pid, obj in man.items():
        base = reg.get(pid, {"id": pid, "traits": {}})
        reg[pid] = _merge(base, obj)
    return reg


def _refresh_traits_cache() -> None:
    try:
        mtime = 0.0
        tf = _traits_file_path()
        if os.path.exists(tf):
            mtime = os.stat(tf).st_mtime
        if _TRAITS_CACHE.get("loaded_mtime") != mtime:
            _TRAITS_CACHE["schema_issues"] = {}
            _TRAITS_CACHE["registry"] = _build_provider_registry()
            _TRAITS_CACHE["loaded_mtime"] = mtime
        else:
            # Always rescan manifests since they can change without touching the traits file
            _TRAITS_CACHE["schema_issues"] = {}
            _TRAITS_CACHE["registry"] = _build_provider_registry()
    except Exception:
        _TRAITS_CACHE["registry"] = {}
        _TRAITS_CACHE["loaded_mtime"] = 0.0


def get_provider_registry() -> Dict[str, Dict[str, Any]]:
    if not _TRAITS_CACHE.get("registry"):
        _refresh_traits_cache()
    return _TRAITS_CACHE.get("registry") or {}


def get_traits_schema_issues() -> Dict[str, Any]:
    return _TRAITS_CACHE.get("schema_issues") or {}


def get_provider_traits(provider_id: Optional[str]) -> Dict[str, Any]:
    if not provider_id:
        return {}
    return get_provider_registry().get(provider_id, {})


def valid_backends() -> set[str]:
    return set(get_provider_registry().keys())


# Valid backend identifiers supported by the core (dynamic)
VALID_BACKENDS = valid_backends()


def active_outbound() -> str:
    """Return the effective outbound backend, normalizing and validating.
    Falls back to legacy fax_backend when dual env is not set or invalid.
    """
    ob = (settings.outbound_backend or settings.fax_backend or "").strip().lower()
    return ob if ob in valid_backends() else settings.fax_backend


def active_inbound() -> str:
    """Return the effective inbound backend, normalizing and validating.
    Falls back to legacy fax_backend when dual env is not set or invalid.
    """
    ib = (settings.inbound_backend or settings.fax_backend or "").strip().lower()
    return ib if ib in valid_backends() else settings.fax_backend


def providerHasTrait(direction: str, trait_name: str) -> bool:
    try:
        pid = active_outbound() if direction == "outbound" else active_inbound()
        if direction == "any":
            return providerHasTrait("outbound", trait_name) or providerHasTrait("inbound", trait_name)
        tr = (get_provider_traits(pid).get("traits") or {})
        val = tr.get(trait_name)
        return bool(val)
    except Exception:
        return False


def providerTraitValue(direction: str, trait_name: str):
    try:
        pid = active_outbound() if direction == "outbound" else active_inbound()
        if direction == "any":
            # Prefer outbound's value, else inbound
            v = providerTraitValue("outbound", trait_name)
            return v if v is not None else providerTraitValue("inbound", trait_name)
        tr = (get_provider_traits(pid).get("traits") or {})
        return tr.get(trait_name)
    except Exception:
        return None


def is_inbound_sip() -> bool:
    # SIP-like inbound if AMI is required by inbound provider
    return providerHasTrait("inbound", "requires_ami")


def is_outbound_cloud() -> bool:
    try:
        pid = active_outbound()
        return (get_provider_traits(pid).get("kind") or "").lower() == "cloud"
    except Exception:
        return False
