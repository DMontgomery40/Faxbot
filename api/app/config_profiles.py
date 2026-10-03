"""Immutable provider configuration captured before accepting fax work."""
from dataclasses import dataclass, field
import hashlib
import json
import re


class ConfigurationRecordError(ValueError):
    """Safe error for invalid private configuration records."""


def _json_value(value):
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError
        for item in value.values():
            _json_value(item)
    elif isinstance(value, list):
        for item in value:
            _json_value(item)
    elif value is not None and type(value) not in (str, bool, int, float):
        raise ValueError


@dataclass(frozen=True, init=False)
class ConfigurationDocument:
    """A private JSON object; callers receive copies rather than shared state."""
    _encoded: str = field(repr=False)

    def __init__(self, document):
        try:
            if not isinstance(document, dict):
                raise ValueError
            _json_value(document)
            encoded = json.dumps(document, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
            if len(encoded.encode('utf-8')) > 1024 * 1024:
                raise ValueError
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise ConfigurationRecordError('Invalid or oversized configuration record.') from None
        object.__setattr__(self, '_encoded', encoded)

    def as_dict(self):
        return json.loads(self._encoded)

    @property
    def digest(self):
        return hashlib.sha256(self._encoded.encode('utf-8')).hexdigest()


@dataclass(frozen=True, init=False)
class ProviderConfiguration:
    provider_id: str
    _document: ConfigurationDocument = field(repr=False)

    def __init__(self, provider_id, *, credentials=None, settings=None, traits=None, manifest=None):
        if (not isinstance(provider_id, str) or len(provider_id) > 255
                or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', provider_id, re.ASCII) is None):
            raise ConfigurationRecordError('Invalid provider identity.')
        mappings = {'credentials': credentials, 'settings': settings, 'traits': traits}
        if any(value is not None and not isinstance(value, dict) for value in mappings.values()):
            raise ConfigurationRecordError('Invalid provider configuration.')
        if manifest is not None and (not isinstance(manifest, dict) or manifest.get('id') != provider_id):
            raise ConfigurationRecordError('Provider manifest identity does not match.')
        document = ConfigurationDocument({'provider_id': provider_id,
            **{key: value if value is not None else {} for key, value in mappings.items()}, 'manifest': manifest})
        object.__setattr__(self, 'provider_id', provider_id)
        object.__setattr__(self, '_document', document)

    @classmethod
    def from_payload(cls, payload):
        if not isinstance(payload, dict) or set(payload) != {'provider_id', 'credentials', 'settings', 'traits', 'manifest'}:
            raise ConfigurationRecordError('Invalid stored provider configuration.')
        return cls(**payload)

    def as_dict(self):
        return self._document.as_dict()

    @property
    def credentials(self):
        return self.as_dict()['credentials']

    @property
    def settings(self):
        return self.as_dict()['settings']

    @property
    def traits(self):
        return self.as_dict()['traits']

    @property
    def manifest(self):
        return self.as_dict()['manifest']

    @property
    def manifest_digest(self):
        manifest = self.manifest
        return ConfigurationDocument(manifest).digest if manifest is not None else None


@dataclass(frozen=True)
class ProviderProfile:
    id: str
    account_id: str
    configuration: ProviderConfiguration = field(repr=False)

    @property
    def credential_revision(self):
        return self.id
