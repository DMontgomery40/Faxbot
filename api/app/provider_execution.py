"""Construct provider adapters from the immutable frame accepted for work."""
from __future__ import annotations

from typing import TYPE_CHECKING

from .config_profiles import ProviderConfiguration, ProviderProfile

if TYPE_CHECKING:
    from .phaxio_service import PhaxioFaxService
    from .plugins.http_provider import HttpProviderRuntime
    from .signalwire_service import SignalWireFaxService
    from .sinch_service import SinchFaxService


class ProviderExecutionError(RuntimeError):
    """Safe failure constructing an adapter from a stored provider frame."""


class UnsupportedProviderExecutionError(ProviderExecutionError):
    """The profile requires an execution owner outside this HTTP factory."""


def _string(values, name):
    value = values.get(name, '')
    if not isinstance(value, str):
        raise ProviderExecutionError('Stored provider configuration cannot be executed.')
    return value


def service_from_profile(profile: ProviderProfile) -> (
        PhaxioFaxService | SinchFaxService | SignalWireFaxService | HttpProviderRuntime):
    """Return a new adapter; never resolve current credentials or installed files.

    A captured HTTP manifest takes precedence over a built-in HTTP adapter.
    Telephony resources remain owned by their worker/lifespan implementation.
    """
    if not isinstance(profile, ProviderProfile) or not isinstance(profile.configuration, ProviderConfiguration):
        raise ProviderExecutionError('Invalid stored provider profile.')
    configuration = profile.configuration
    identity = configuration.provider_id
    if identity in {'sip', 'freeswitch'}:
        raise UnsupportedProviderExecutionError('Provider requires a telephony execution owner.')

    credentials = configuration.credentials
    settings = configuration.settings
    manifest = configuration.manifest
    try:
        if manifest is not None:
            from .plugins.http_provider import HttpManifest, HttpProviderRuntime
            return HttpProviderRuntime(HttpManifest.from_dict(manifest), credentials, settings)
        if identity == 'phaxio':
            from .phaxio_service import PhaxioFaxService
            return PhaxioFaxService(api_key=_string(credentials, 'api_key'),
                api_secret=_string(credentials, 'api_secret'), status_callback_url=_string(settings, 'callback_url'))
        if identity == 'sinch':
            from .sinch_service import SinchFaxService
            return SinchFaxService(project_id=_string(settings, 'project_id'),
                api_key=_string(credentials, 'api_key'), api_secret=_string(credentials, 'api_secret'),
                base_url=_string(settings, 'base_url'))
        if identity == 'signalwire':
            from .signalwire_service import SignalWireFaxService
            return SignalWireFaxService(space_url=_string(settings, 'space_url'),
                project_id=_string(settings, 'project_id'), api_token=_string(credentials, 'api_token'),
                from_number=_string(settings, 'fax_from_e164'), status_callback_url=_string(settings, 'callback_url'))
    except ProviderExecutionError:
        raise
    except (TypeError, ValueError, AttributeError):
        raise ProviderExecutionError('Stored provider configuration cannot be executed.') from None
    raise UnsupportedProviderExecutionError('Provider has no supported captured HTTP adapter.')
