"""Build an extra route's provider account from the revision a fax was accepted under.

The configuration comes from that immutable revision, exactly as configuration
activation would build it, so an accepted fax never picks up later credentials.
"""
from pathlib import Path
import re

from ..config_profiles import ProviderProfile
from ..provider_execution import ProviderExecutionError, service_from_profile


class RouteUnavailable(RuntimeError):
    """This route cannot be used for the fax right now."""


_CATALOGS = {}


def _catalog(revision):
    from ..config_activation import _catalog as load_catalog
    catalog = _CATALOGS.get(revision.id)
    if catalog is None:
        catalog = load_catalog(revision.values)
        if len(_CATALOGS) > 32:
            _CATALOGS.clear()
        _CATALOGS[revision.id] = catalog
    return catalog


def route_configuration(revision, provider_id):
    """The provider configuration for ``provider_id`` captured in ``revision``."""
    from ..config_activation import ConfigurationActivationError, _configuration_for, _effective_definition
    if provider_id not in revision.values.outbound_route_providers:
        raise RouteUnavailable('This route is not listed for the fax.')
    try:
        catalog = _catalog(revision)
        if provider_id not in catalog.provider_ids:
            raise RouteUnavailable('This provider is not installed.')
        definition = _effective_definition(revision.values, catalog.get(provider_id))
        state = revision.plugins.as_dict()
        settings = state.get('settings', {}).get(provider_id, {}) if isinstance(state.get('settings'), dict) else {}
        configuration = _configuration_for(revision.values, definition, settings)
    except (ConfigurationActivationError, ValueError, KeyError, OSError):
        raise RouteUnavailable('This route is not fully set up.') from None
    manifest = configuration.manifest
    if manifest is not None and 'send_fax' not in manifest.get('actions', {}):
        raise RouteUnavailable('This provider cannot send faxes.')
    return configuration


def route_needs_tiff(configuration):
    """Match acceptance: native SIP engines and providers with the requires_tiff trait."""
    return ((configuration.manifest is None and configuration.provider_id in {'sip', 'freeswitch'})
            or configuration.traits.get('requires_tiff') is True)


def ensure_route_artifact(revision, configuration, job_id):
    """Rasterize the accepted PDF when this route needs a fax TIFF the fax does not have yet.

    The original PDF is never changed; the TIFF is written atomically beside it,
    where the captured transport and artifact cleanup already expect it.
    """
    if not route_needs_tiff(configuration):
        return None
    if not isinstance(job_id, str) or re.fullmatch('[a-f0-9]{32}', job_id) is None:
        raise RouteUnavailable('This fax has no usable document.')
    root = Path(revision.values.fax_data_dir)
    pdf, tiff = root / (job_id + '.pdf'), root / (job_id + '.tiff')
    if tiff.is_symlink() or pdf.is_symlink():
        raise RouteUnavailable('This fax has no usable document.')
    if tiff.is_file():
        return tiff
    if not pdf.is_file():
        raise RouteUnavailable('This fax has no usable document.')
    from ..conversion import DocumentConversionError, pdf_to_tiff
    try:
        pdf_to_tiff(str(pdf), str(tiff))
    except DocumentConversionError:
        raise RouteUnavailable('This route needs a fax image that could not be prepared.') from None
    return tiff


def route_ready(configuration, *, ami=None):
    """Local readiness only; never contacts the provider."""
    pid = configuration.provider_id
    if configuration.manifest is not None or pid not in {'sip', 'freeswitch'}:
        try:
            return bool(service_from_profile(ProviderProfile('route', 'route', configuration)).is_configured())
        except (ProviderExecutionError, ValueError, TypeError):
            return False
    if pid == 'sip':
        return ami is not None and ami._connected.is_set()
    from ..freeswitch_service import fs_cli_available
    return bool(fs_cli_available())
