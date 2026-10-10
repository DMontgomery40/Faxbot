"""Provider accounts: every account Faxbot sends and receives with, trunks included (provider-rules design §3).

An account is one account at one provider: a short key, the provider, a label, its credentials and settings, an
optional site, two switches (sends faxes, receives faxes), whether it is on, the numbers it receives on and its
limits (faxes or lines at once, calls a second on a trunk, and a daily spending limit).

- **Primary accounts.** The first account of each provider has the provider id as its key (``sip``,
  ``humblefax``, ``sinch``) and is read from that provider's own settings, the flat ``ConfigurationValues``
  fields, exactly as before accounts existed. It is never stored as an account. What the account list itself
  changes on a primary account (on or off, its site, label, daily limit, and for a provider other than the trunk
  its numbers) is stored as a small overlay under its key, and only once someone changes it.
- **Extra accounts** (a second Sinch account, a second trunk) live in the configuration revision under its
  ``accounts`` key, credentials included, so a fax accepted under a revision never picks up later credentials
  (§3.5). A configuration with no extra account and no overlay stores no ``accounts`` key at all, so it reads,
  builds profiles and digests exactly as before.
- **Defaults.** The default sending account is the provider in ``FAX_OUTBOUND_BACKEND``/``FAX_BACKEND``, or an
  extra account marked ``default_sending``; the default receiving account likewise from ``FAX_INBOUND_BACKEND``.
  An extra default account sets that variable to its provider, and configuration activation builds the role's
  profile from the extra account (``config_activation.compile_profiles``).

Every function here reads an immutable revision (``revision.values`` carries the revision's accounts as a
read-only view) and never contacts a provider. Health comes from records Faxbot already keeps.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import re

import sqlalchemy as sa


KEY = re.compile(r'[a-z0-9][a-z0-9_-]{0,31}')
# 'digital' names every Direct message and FHIR route in sending rules (rules.model.DIGITAL).
RESERVED = ('local', 'direct', 'digital')
# Providers an extra account can be added for, with the settings each asks for: (name, the provider's own
# configuration field, label, required for an extra account, help). Secrets are the fields the configuration
# model marks secret; they are write-only.
FIELDS = {
    'sinch': (
        ('project_id', 'sinch_project_id', 'Project ID', True, None),
        ('api_key', 'sinch_api_key', 'Access key ID', True, None),
        ('api_secret', 'sinch_api_secret', 'Access key secret', True, None),
        ('base_url', 'sinch_base_url', 'Sinch address', False, "Leave it empty to use Sinch's usual address."),
        ('inbound_basic_user', 'sinch_inbound_basic_user', 'User name for received faxes', False,
         'Sinch sends it with each received fax. Without one, Faxbot checks each received fax with Sinch first.'),
        ('inbound_basic_pass', 'sinch_inbound_basic_pass', 'Password for received faxes', False, None),
    ),
    'phaxio': (
        ('api_key', 'phaxio_api_key', 'API key', True, None),
        ('api_secret', 'phaxio_api_secret', 'API secret', True, None),
        ('callback_token', 'phaxio_callback_token', 'Callback token', False,
         "From Phaxio's API settings. Faxbot uses it to check that each received fax came from Phaxio."),
        ('inbound_verify_signature', 'phaxio_inbound_verify_signature',
         'Check that each received fax came from Phaxio', False, None),
    ),
    'humblefax': (
        ('access_key', 'humblefax_access_key', 'Access key', True, None),
        ('secret_key', 'humblefax_secret_key', 'Secret key', True, None),
        ('from_number', 'humblefax_from_number', 'Fax number', False, None),
        ('poll_seconds', 'humblefax_poll_seconds', 'Check for received faxes every (seconds)', False, None),
    ),
    'efax': (
        ('app_id', 'efax_app_id', 'App ID', True, None),
        ('api_key', 'efax_api_key', 'API key', True, None),
        ('user_id', 'efax_user_id', 'User ID', True, None),
        ('caller_id', 'efax_caller_id', 'Fax number recipients see', False, None),
        ('csid', 'efax_csid', 'Station name on each page', False, None),
        ('webhook_secret', 'efax_webhook_secret', 'Notification secret', False,
         'With it, Faxbot checks eFax as soon as eFax says a fax arrived; without it, Faxbot checks every minute.'),
        ('poll_seconds', 'efax_poll_seconds', 'Check for received faxes every (seconds)', False, None),
        ('delete_after_download', 'efax_delete_after_download', 'Delete each fax from eFax once Faxbot has it',
         False, None),
    ),
    'signalwire': (
        ('space_url', 'signalwire_space_url', 'Space address', True, None),
        ('project_id', 'signalwire_project_id', 'Project ID', True, None),
        ('api_token', 'signalwire_api_token', 'API token', True, None),
        ('fax_from_e164', 'signalwire_fax_from_e164', 'Fax number', True, None),
    ),
    'documo': (
        ('api_key', 'documo_api_key', 'API key', True, None),
    ),
    'sip': (
        ('preset', 'sip_trunk_preset', 'Carrier', False, None),
        ('host', 'sip_trunk_host', 'Server', True, None),
        ('port', 'sip_trunk_port', 'Port', False, "Leave it empty for the carrier's usual port."),
        ('transport', 'sip_trunk_transport', 'Connection', False, None),
        ('auth', 'sip_trunk_auth', 'How Faxbot signs in', False,
         'registration when the carrier gave you a user name and password; ip when it recognizes your address.'),
        ('username', 'sip_trunk_username', 'User name', False, None),
        ('password', 'sip_trunk_password', 'Password', False, None),
        ('caller_id', 'sip_trunk_caller_id', 'Caller ID', False, None),
        ('outbound_proxy', 'sip_trunk_outbound_proxy', 'Outbound proxy', False, None),
        # Each trunk's own fax and number settings (WP-T), so a second trunk never inherits the first one's.
        ('t38', 'sip_t38_enabled', 'Fax over IP (T.38)', False,
         'Leave it on unless this carrier turns fax calls into audio itself.'),
        ('answer_cap', 'sip_fax_answer_cap', 'Hang up when no fax machine answers within 50 seconds', False,
         'Leave it on: where this carrier bills by the whole minute, a call no fax machine answers costs one minute '
         'instead of two.'),
        ('codecs', 'sip_trunk_codecs', 'Audio codecs', False,
         "ulaw, alaw or both in order; leave it empty for the carrier's usual order."),
        ('dial_format', 'sip_trunk_dial_format', 'Number format', False,
         'e164 for +44 numbers, or local to dial numbers the way a phone here dials them.'),
        ('dial_prefix', 'sip_trunk_dial_prefix', 'Outside-line prefix', False,
         'Only with the local number format, such as 9.'),
        ('own_access', 'sip_trunk_own_access', "Your own line's internet address", False,
         'Telekom CompanyFlex only: the address or range of your Telekom line, such as 203.0.113.7.'),
        ('api_key', 'telnyx_api_key', 'Telnyx API key', False,
         "Only for a Telnyx trunk: Faxbot reads this trunk's numbers' fax over IP settings with it."),
    ),
}
# The order primary accounts are listed in after the default and listed routes.
PROVIDER_ORDER = ('sip', 'humblefax', 'sinch', 'phaxio', 'efax', 'signalwire', 'documo', 'freeswitch')
# Paths where each provider's received-fax notifications arrive; an extra account's is this plus /<key>.
WEBHOOK_PATHS = {'sinch': '/sinch-inbound', 'phaxio': '/phaxio-inbound', 'efax': '/efax-inbound'}
POLLING = ('humblefax', 'efax')
HEALTH_STATES = ('ready', 'not_set_up', 'off', 'waiting', 'failing', 'spending_limit')
# Failing: at least this many of the last attempts, and most of them, failed.
FAILING_WINDOW, FAILING_MINIMUM = 10, 3
MICROS = 1_000_000


class AccountsError(ValueError):
    """One plain sentence for the administrator; never a credential."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class ProviderAccount:
    key: str
    provider: str
    label: str
    primary: bool
    sends: bool
    receives: bool
    enabled: bool = True
    site: str | None = None
    numbers: tuple = ()
    at_once: int | None = None
    calls_per_second: int | None = None
    daily_spend_micros: int | None = None
    currency: str | None = None
    default_sending: bool = False
    default_receiving: bool = False
    # The automatic choice may use it: the default sending account and FAX_OUTBOUND_ROUTES.
    automatic: bool = False
    # Everything its provider needs to send or receive is filled in.
    set_up: bool = False
    missing: tuple = ()
    settings: dict = field(default_factory=dict, compare=False, repr=False)
    secrets_set: tuple = ()


# -- reading the configuration ------------------------------------------------------------------------------

def _model_field(name):
    from .config_values import ConfigurationValues
    return ConfigurationValues.model_fields[name]


def is_secret(value_field):
    return bool((_model_field(value_field).json_schema_extra or {}).get('secret'))


def _default(value_field):
    return _model_field(value_field).default


def _kind(value_field):
    default = _default(value_field)
    return bool if isinstance(default, bool) else int if isinstance(default, int) else str


def documents(values):
    """The stored account documents, {key: document}: extra accounts and overlays on primary accounts."""
    return getattr(values, 'provider_accounts', None) or {}


def _catalog_traits(provider):
    try:
        from .config import get_provider_traits
        return get_provider_traits(provider) or {}
    except Exception:
        return {}


def supports_inbound(provider):
    if provider in ('sip', 'humblefax', 'efax', 'sinch', 'phaxio'):
        return True
    return bool(_catalog_traits(provider).get('supports_inbound'))


def provider_name(provider, values=None):
    """The provider's plain name ("Sinch"; the trunk by its carrier, "Telnyx")."""
    from .provider_labels import provider_label, trunk_name
    if provider == 'sip':
        return trunk_name(getattr(values, 'sip_trunk_preset', None) if values is not None else None)
    return provider_label(provider)


def _primary_set_up(values, provider):
    """(set up, labels of what is missing) for a provider's primary account."""
    if provider == 'sip':
        from . import sip_trunk
        if sip_trunk.configured(values):
            return True, ()
        return False, ('Server',)
    fields = FIELDS.get(provider)
    if fields is None:
        return provider == values.effective_outbound or provider in values.outbound_route_providers, ()
    missing = tuple(label for name, value_field, label, required, _ in fields
                    if required and not getattr(values, value_field, None))
    return not missing, missing


def _primary_receives(values, provider):
    if provider == 'sip':
        from .inbound.sip_handover import receives_over_trunk
        return receives_over_trunk(values)
    if provider == 'humblefax':
        return bool(values.humblefax_receive_enabled or values.effective_inbound == 'humblefax')
    return values.effective_inbound == provider and supports_inbound(provider)


def _override(docs, flag):
    return next((key for key, doc in sorted(docs.items()) if doc.get('provider') and doc.get(flag)), None)


def default_sending_key(values):
    """The default sending account's key, or None when no provider is set up for sending."""
    return _override(documents(values), 'default_sending') or values.effective_outbound or None


def default_receiving_key(values):
    return _override(documents(values), 'default_receiving') or values.effective_inbound or None


def _number_list(values, numbers):
    from .routing.numbers import stored_number
    country = getattr(values, 'fax_default_country', 'US') or 'US'
    found = []
    for number in numbers or ():
        text = stored_number(str(number).strip(), country=country) if str(number).strip() else ''
        if text and text not in found:
            found.append(text)
    return tuple(found)


def _limits(doc):
    limits = doc.get('limits') if isinstance(doc.get('limits'), dict) else {}
    return {'at_once': limits.get('at_once'), 'calls_per_second': limits.get('calls_per_second'),
            'daily_spend_micros': limits.get('daily_spend_micros'), 'currency': limits.get('currency')}


def _primary(values, provider, docs, extra_default_sending, extra_default_receiving):
    overlay = docs.get(provider) or {}
    set_up, missing = _primary_set_up(values, provider)
    default_sending = not extra_default_sending and provider == values.effective_outbound
    routes = values.outbound_route_providers
    sends = default_sending or provider in routes or set_up
    settings, secrets = {}, []
    for name, value_field, *_ in FIELDS.get(provider, ()):
        value = getattr(values, value_field, None)
        if is_secret(value_field):
            if value:
                secrets.append(name)
        else:
            settings[name] = value
    limits = _limits(overlay)
    if provider == 'sip':
        numbers = _number_list(values, values.sip_trunk_did_list)
        limits['at_once'] = values.sip_trunk_max_calls or None
        limits['calls_per_second'] = values.sip_trunk_calls_per_second or None
    else:
        numbers = _number_list(values, overlay.get('numbers'))
    return ProviderAccount(
        key=provider, provider=provider, label=overlay.get('label') or provider_name(provider, values), primary=True,
        sends=bool(sends), receives=_primary_receives(values, provider), enabled=overlay.get('enabled', True) is not False,
        site=overlay.get('site') or None, numbers=numbers, at_once=limits['at_once'],
        calls_per_second=limits['calls_per_second'], daily_spend_micros=limits['daily_spend_micros'],
        currency=limits['currency'], default_sending=default_sending,
        default_receiving=not extra_default_receiving and provider == values.effective_inbound,
        automatic=default_sending or provider in routes, set_up=set_up, missing=missing, settings=settings,
        secrets_set=tuple(secrets))


def _extra(values, key, doc):
    provider = doc['provider']
    credentials = doc.get('credentials') or {}
    settings = dict(doc.get('settings') or {})
    missing = tuple(label for name, value_field, label, required, _ in FIELDS.get(provider, ())
                    if required and not (credentials.get(name) if is_secret(value_field) else settings.get(name)))
    limits = _limits(doc)
    return ProviderAccount(
        key=key, provider=provider, label=doc.get('label') or f'{provider_name(provider, values)} ({key})',
        primary=False, sends=doc.get('sends', True) is not False, receives=bool(doc.get('receives')),
        enabled=doc.get('enabled', True) is not False, site=doc.get('site') or None,
        numbers=_number_list(values, doc.get('numbers')), at_once=limits['at_once'],
        calls_per_second=limits['calls_per_second'], daily_spend_micros=limits['daily_spend_micros'],
        currency=limits['currency'], default_sending=bool(doc.get('default_sending')),
        default_receiving=bool(doc.get('default_receiving')), automatic=bool(doc.get('default_sending')),
        set_up=not missing, missing=missing, settings=settings,
        secrets_set=tuple(sorted(name for name, value in credentials.items() if value)))


def _is_extra(key, doc):
    # A digital account (a HISP account or FHIR client, ``digital/accounts.py``) shares the document but is never a
    # fax account: it is not a sending account, a provider route or a receiving number.
    return (isinstance(doc, dict) and bool(doc.get('provider')) and doc.get('provider') != key
            and doc.get('kind') != 'digital')


def all_accounts(values):
    """Every account: the default sending account first, then FAX_OUTBOUND_ROUTES in order, then the rest."""
    docs = documents(values)
    extras = {key: doc for key, doc in docs.items() if _is_extra(key, doc)}
    sending_override = _override(extras, 'default_sending')
    receiving_override = _override(extras, 'default_receiving')
    order = []

    def add(provider):
        if provider and provider not in order and provider not in extras:
            order.append(provider)
    if not sending_override:
        add(values.effective_outbound)
    for provider in values.outbound_route_providers:
        add(provider)
    if not receiving_override:
        add(values.effective_inbound)
    for provider in PROVIDER_ORDER:
        if provider in order:
            continue
        set_up, _ = _primary_set_up(values, provider)
        if (set_up and provider in FIELDS) or _primary_receives(values, provider) or provider in docs:
            add(provider)
    found = [_primary(values, provider, docs, sending_override, receiving_override) for provider in order]
    built = [_extra(values, key, doc) for key, doc in sorted(extras.items())]
    if sending_override:
        found = [account for account in built if account.key == sending_override] + found
        built = [account for account in built if account.key != sending_override]
    return tuple(found + built)


def account_named(values, key):
    return next((account for account in all_accounts(values) if account.key == key), None)


def extra_accounts(values):
    return tuple(account for account in all_accounts(values) if not account.primary)


def receiving_accounts(values):
    """Accounts that receive faxes now: on, receiving switched on, and receiving turned on in Settings."""
    if not getattr(values, 'inbound_enabled', False):
        return ()
    return tuple(account for account in all_accounts(values) if account.receives and account.enabled)


def extra_receiving(values):
    """Whether any extra account receives: until one does, the receiving route gate is exactly the old one."""
    return any(not account.primary and account.receives for account in all_accounts(values))


def sending_accounts(values):
    """The sending accounts as the rules engine reads them (``rules.model.Account``), in configured order."""
    from .rules import model
    values = getattr(values, 'values', values)  # a revision or its values
    found = []
    for account in all_accounts(values):
        if not account.sends or KEY.fullmatch(account.key) is None or account.key in RESERVED:
            continue
        sslfax = account.provider == 'sip' and bool(getattr(values, 'sip_sslfax_enabled', False)) and account.primary
        found.append(model.Account(account.key, account.provider, account.label, sends=True, enabled=account.enabled,
                                   default=account.default_sending, automatic=account.automatic, site=account.site,
                                   sslfax=sslfax))
    return tuple(found)


def account_permitted(revision, key, envelope=None):
    """Whether a delivery attempt may be bound to this account: it sends in the fax's revision and, when the fax
    has a route envelope, the envelope allows it. Whether it is on right now is ``account_enabled``."""
    values = getattr(revision, 'values', revision)
    if not any(account.key == key for account in sending_accounts(values)):
        return False
    if envelope is not None:
        allowed = getattr(envelope, 'accounts', envelope)
        return key in tuple(allowed or ())
    return True


def account_enabled(values, key):
    """Whether the account is on in the current configuration; off stops new attempts at once."""
    account = account_named(values, key)
    return account is not None and account.enabled


# -- one account's own settings -----------------------------------------------------------------------------

def _patch_for(provider, doc):
    """{configuration field: value} that gives an extra account's settings and credentials, every other field
    of its provider at its default, so nothing leaks from the primary account."""
    from .config_plugin_fields import PLUGIN_FIELDS
    settings, credentials = doc.get('settings') or {}, doc.get('credentials') or {}
    patch = {}
    if provider != 'sip':
        # The trunk's plugin fields are the shared fax engine's (Asterisk), not the account's: kept as they are.
        # Every other provider field starts at its default. Sinch's key fields are written explicitly, so they
        # never fall back to the older Phaxio variables.
        for value_field in PLUGIN_FIELDS.get(provider, {}).values():
            patch[value_field] = _default(value_field)
    for name, value_field, *_ in FIELDS.get(provider, ()):
        given = credentials.get(name) if is_secret(value_field) else settings.get(name)
        default = _default(value_field)
        patch[value_field] = default if given is None or given == '' else given
    if provider == 'sip':
        limits = _limits(doc)
        patch['sip_trunk_dids'] = ','.join(doc.get('numbers') or ())
        patch['sip_trunk_max_calls'] = limits['at_once'] or 0
        patch['sip_trunk_calls_per_second'] = limits['calls_per_second'] or 0
    if provider == 'humblefax':
        patch['humblefax_receive_enabled'] = bool(doc.get('receives'))
    return patch


def account_values(values, key):
    """Configuration values as this account sees them: its provider's settings replaced by its own.

    A primary account's (a provider id, listed or not) are the values themselves. Raises AccountsError when an
    extra account's settings can't be read."""
    docs = documents(values)
    doc = docs.get(key)
    if not _is_extra(key, doc):
        return values
    try:
        return values.with_patch(_patch_for(doc['provider'], doc))
    except ValueError as error:
        raise AccountsError(_value_sentence(doc['provider'], error)) from None


def values_from_configuration(values, configuration):
    """Configuration values with one provider's fields taken from a captured ProviderConfiguration (a provider
    profile), for fetching a received fax with the account it was bound to. None when they can't be read."""
    from .config_plugin_fields import PLUGIN_FIELDS
    mapping = PLUGIN_FIELDS.get(configuration.provider_id)
    if not mapping:
        return None
    captured = {**configuration.settings, **configuration.credentials}
    patch = {field_name: captured[name] for name, field_name in mapping.items()
             if name in captured and isinstance(captured[name], (str, int, bool))}
    try:
        return values.with_patch(patch)
    except ValueError:
        return None


def _value_sentence(provider, error):
    from .config_values import ConfigurationValueError
    names = {}
    for name, value_field, label, *_ in FIELDS.get(provider, ()):
        alias = _model_field(value_field).validation_alias
        names[getattr(alias, 'choices', [alias])[0]] = label
    if isinstance(error, ConfigurationValueError):
        labels = [names.get(issue['field'], 'a setting') for issue in error.issues]
        if labels:
            return f"Check the {', '.join(dict.fromkeys(labels))}: Faxbot cannot use what was entered."
    return 'Check the settings: Faxbot cannot use what was entered.'


_CONFIGURATIONS = {}


def account_configuration(values, key, *, catalog=None, plugin_state=None):
    """The ``ProviderConfiguration`` of one account, built as configuration activation builds a provider's.

    A primary account's comes from the flat fields. An extra account's comes from its own settings, and a trunk
    account's settings also name its key (``trunk``) so the dialing code knows its endpoint. Raises AccountsError.
    """
    from .config_activation import ConfigurationActivationError, _catalog, _configuration_for, _effective_definition
    doc = documents(values).get(key)
    account = account_named(values, key)
    if account is None:
        raise AccountsError('Faxbot has no account with this key.', 404)
    own = account_values(values, key)
    try:
        catalog = catalog or _catalog(values)
        if account.provider not in catalog.provider_ids:
            raise AccountsError('This provider is not installed.')
        definition = _effective_definition(values, catalog.get(account.provider))
        state = plugin_state if plugin_state is not None else {}
        settings = state.get('settings', {}).get(account.provider, {}) if isinstance(state.get('settings'), dict) else {}
        configuration = _configuration_for(own, definition, settings)
    except (ConfigurationActivationError, ValueError, KeyError, OSError) as error:
        if isinstance(error, AccountsError):
            raise
        raise AccountsError('This account is not fully set up.') from None
    if _is_extra(key, doc) and account.provider == 'sip':
        from .config_profiles import ProviderConfiguration
        data = configuration.as_dict()
        configuration = ProviderConfiguration(account.provider, credentials=data['credentials'],
                                              settings={**data['settings'], 'trunk': key}, traits=data['traits'],
                                              manifest=data['manifest'])
    return configuration


def route_configuration(revision, key):
    """The ProviderConfiguration for sending by account ``key`` under the revision a fax was accepted with.

    A primary account is built exactly as before (``routing.routes.route_configuration``); an extra account from
    its own settings in that revision. Raises ``routing.routes.RouteUnavailable``.
    """
    from .routing.routes import RouteUnavailable, _catalog
    from .routing.routes import route_configuration as provider_route
    values = revision.values
    account = account_named(values, key)
    if account is None or not account.sends:
        raise RouteUnavailable('This route is not listed for the fax.')
    if account.primary:
        return provider_route(revision, key)
    cache_key = (revision.id, key)
    if cache_key in _CONFIGURATIONS:
        return _CONFIGURATIONS[cache_key]
    try:
        configuration = account_configuration(values, key, catalog=_catalog(revision),
                                              plugin_state=revision.plugins.as_dict())
    except AccountsError:
        raise RouteUnavailable('This route is not fully set up.') from None
    manifest = configuration.manifest
    if manifest is not None and 'send_fax' not in manifest.get('actions', {}):
        raise RouteUnavailable('This provider cannot send faxes.')
    if len(_CONFIGURATIONS) > 64:
        _CONFIGURATIONS.clear()
    _CONFIGURATIONS[cache_key] = configuration
    return configuration


def default_override(accounts_document, role):
    """The extra account a role's profile is built from (``default_sending``/``default_receiving``), or None."""
    docs = accounts_document.as_dict() if hasattr(accounts_document, 'as_dict') else (accounts_document or {})
    flag = 'default_sending' if role == 'outbound' else 'default_receiving'
    return _override({key: doc for key, doc in docs.items() if _is_extra(key, doc)}, flag)


# -- receiving addresses --------------------------------------------------------------------------------------

def original_path_account(values, provider):
    """The account a provider's original receiving address (``/sinch-inbound``) serves, or None.

    While no extra account receives, the provider's primary account, under the old route gate. After that: the
    default receiving account when it is this provider's, else this provider's primary account when it receives
    and is on, else this provider's only receiving account.
    """
    accounts = [account for account in all_accounts(values) if account.provider == provider]
    if not extra_receiving(values):
        return next((account for account in accounts if account.primary), None) or (
            ProviderAccount(key=provider, provider=provider, label=provider_name(provider, values), primary=True,
                            sends=False, receives=False))
    receiving = [account for account in accounts if account.receives and account.enabled]
    default = default_receiving_key(values)
    for account in receiving:
        if account.key == default:
            return account
    primary = next((account for account in receiving if account.primary), None)
    if primary is not None:
        return primary
    return receiving[0] if len(receiving) == 1 else None


def receiving_account(values, provider, path_key=None):
    """The account a receiving address serves: ``/<provider>-inbound`` with ``path_key`` None, else
    ``/<provider>-inbound/<path_key>``. None when the address serves no account that receives and is on; the
    caller answers 404 and writes an audit row."""
    if path_key is None:
        return original_path_account(values, provider)
    account = account_named(values, path_key)
    if account is None or account.provider != provider or not account.receives or not account.enabled:
        return None
    return account


def receiving_path(values, account):
    """The path its provider posts this account's received faxes to, or None when it receives another way."""
    base = WEBHOOK_PATHS.get(account.provider)
    if base is None or not account.receives:
        return None
    if account.provider == 'efax' and not account_values(values, account.key).efax_webhook_secret:
        return None
    served = original_path_account(values, account.provider)
    return base if served is not None and served.key == account.key else f'{base}/{account.key}'


def webhook_address(values, account):
    """The full address to give the provider for received faxes, or None."""
    path = receiving_path(values, account)
    if path is None:
        return None
    base = values.public_api_url
    if account.provider == 'sinch':
        base = account_values(values, account.key).sinch_webhook_base_url or base
    return base.rstrip('/') + path


# -- spending and health --------------------------------------------------------------------------------------

def local_midnight(values, now=None):
    """The last midnight in the installation's time zone, as naive UTC."""
    from .people_time import zone
    now = now or datetime.utcnow()
    local = now.replace(tzinfo=timezone.utc).astimezone(zone(getattr(values, 'time_zone', '') or ''))
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(timezone.utc).replace(tzinfo=None)


def _table(engine, name):
    try:
        return sa.Table(name, sa.MetaData(), autoload_with=engine)
    except sa.exc.SQLAlchemyError:
        return None


def spent_today(engine, values, account, *, now=None):
    """What the account's attempts since local midnight cost, in the limit's currency (micros); 0 without records.

    Each attempt counts once: its settled amount, else what the provider reported, else Faxbot's estimate.
    """
    costs = _table(engine, 'delivery_attempt_costs') if engine is not None else None
    if costs is None:
        return 0
    since = local_midnight(values, now)
    amount = sa.func.coalesce(costs.c.settled_cost_micros, costs.c.reported_cost_micros,
                              costs.c.estimated_cost_micros)
    currency = sa.func.coalesce(costs.c.reported_currency, costs.c.currency)
    query = sa.select(sa.func.coalesce(sa.func.sum(amount), 0)).where(costs.c.route == account.key,
                                                                      costs.c.created_at >= since)
    if account.currency:
        query = query.where(currency == account.currency)
    try:
        with engine.connect() as connection:
            return int(connection.execute(query).scalar() or 0)
    except sa.exc.SQLAlchemyError:
        return 0


def over_daily_limit(engine, values, account, *, now=None):
    """Whether the account reached its daily spending limit; it is usable again after local midnight."""
    if account.daily_spend_micros is None:
        return False
    return spent_today(engine, values, account, now=now) >= account.daily_spend_micros


def _recent_outcomes(engine, key):
    costs = _table(engine, 'delivery_attempt_costs') if engine is not None else None
    if costs is None:
        return []
    try:
        with engine.connect() as connection:
            return list(connection.execute(sa.select(costs.c.outcome).where(
                costs.c.route == key, costs.c.outcome.in_(('success', 'failed')))
                .order_by(costs.c.created_at.desc()).limit(FAILING_WINDOW)).scalars())
    except sa.exc.SQLAlchemyError:
        return []


def _has_received(engine, account):
    imports = _table(engine, 'inbound_imports') if engine is not None else None
    if imports is None:
        return False
    condition = imports.c.account_key == account.key if 'account_key' in imports.c else sa.false()
    if account.primary:
        # Faxes received before accounts existed carry no account key; they came in on the primary account.
        legacy = sa.and_(imports.c.source == account.provider, imports.c.account_key.is_(None)) \
            if 'account_key' in imports.c else imports.c.source == account.provider
        condition = sa.or_(condition, legacy)
    try:
        with engine.connect() as connection:
            return connection.execute(sa.select(imports.c.id).where(condition).limit(1)).first() is not None
    except sa.exc.SQLAlchemyError:
        return False


def _money_text(micros, currency):
    from .routing.costs import money_text
    return money_text(micros, currency or 'USD')


def health(values, account, engine, *, now=None, problem=None):
    """(state, sentence, details) for one account, from records Faxbot already keeps.

    ``problem`` is a receiver's last plain sentence (a poller that could not check), if any.
    """
    details = []
    if account.receives:
        address = webhook_address(values, account)
        path = receiving_path(values, account)
        if address:
            details.append(f'Give {provider_name(account.provider, values)} this address for received faxes: {address}')
            if path and path != WEBHOOK_PATHS.get(account.provider):
                details.append(f'If received faxes reach Faxbot through a tunnel that passes only listed addresses, '
                               f'add {path} to that list.')
        elif account.provider in POLLING:
            details.append(f'Faxbot asks {provider_name(account.provider, values)} for received faxes; there is no '
                           'address to give it.')
    if account.numbers:
        details.append('Receives on ' + ', '.join(account.numbers) + '.')
    if not account.enabled:
        return 'off', "Faxbot doesn't send or receive with it until you turn it back on.", details
    if not account.set_up:
        missing = ', '.join(account.missing) or 'settings'
        return 'not_set_up', f'Add its {missing} to finish setting it up.', details
    if account.provider == 'sip' and not account.primary:
        # A trunk after the first that Asterisk's file leaves out (sip_trunk.trunk_problems) carries no call.
        from .sip_trunk import trunk_problems
        problem = trunk_problems(values).get(account.key)
        if problem:
            return 'not_set_up', problem, details
    if over_daily_limit(engine, values, account, now=now):
        limit = _money_text(account.daily_spend_micros, account.currency)
        return ('spending_limit', f'It has cost {limit} today, its daily limit; Faxbot uses it again after '
                'midnight.', details)
    outcomes = _recent_outcomes(engine, account.key) if account.sends else []
    failed = outcomes.count('failed')
    if failed >= FAILING_MINIMUM and failed * 2 > len(outcomes):
        return ('failing', f'{failed} of its last {len(outcomes)} faxes failed. Check its settings with '
                f'{provider_name(account.provider, values)}.', details)
    if problem and account.receives:
        return 'failing', problem, details
    if account.receives and not values.inbound_enabled:
        details.append('Receiving is turned off in Settings, so no fax arrives on any account.')
    elif account.receives and not _has_received(engine, account):
        return 'waiting', 'No fax has arrived on it yet.', details
    return 'ready', 'Nothing needs your attention.', details


# -- changing accounts ----------------------------------------------------------------------------------------

def changed_keys(before, after):
    """The keys of the accounts that were added, changed or removed, for the audit row."""
    return sorted(key for key in set(before) | set(after) if before.get(key) != after.get(key))


def _sentence_label(values, provider):
    return provider_name(provider, values)


def _clean_number_list(values, numbers):
    from .routing.numbers import InvalidNumber, normalize_number
    if numbers is None:
        return []
    if not isinstance(numbers, (list, tuple)) or len(numbers) > 200:
        raise AccountsError('List at most 200 fax numbers.')
    country = getattr(values, 'fax_default_country', 'US') or 'US'
    found = []
    for number in numbers:
        if not isinstance(number, str) or not number.strip():
            continue
        try:
            text = normalize_number(number.strip(), country=country)
        except (InvalidNumber, ValueError):
            raise AccountsError(f'{str(number)[:40]} is not a fax number Faxbot can read. Write it with its '
                                'country code, such as +13035550100.') from None
        if text not in found:
            found.append(text)
    return found


def _clean_limits(values, limits, provider, current=None):
    from .routing.costs import InvalidRateCard, parse_amount
    current = dict(current or {})
    if limits is None:
        return current
    if not isinstance(limits, dict):
        raise AccountsError('Give the limits as faxes at once, calls a second and a daily spending limit.')
    result = {}
    for name, top in (('at_once', 200), ('calls_per_second', 100)):
        value = limits.get(name, current.get(name))
        if value in (None, 0, ''):
            result[name] = None
            continue
        if type(value) is not int or not 0 < value <= top:
            raise AccountsError(f'Enter a whole number from 1 to {top}, or leave it empty for no limit.')
        result[name] = value
    if result.get('calls_per_second') and provider != 'sip':
        raise AccountsError('Calls a second applies only to a trunk.')
    if 'daily_limit' in limits:
        money = limits['daily_limit']
        if money is None:
            result['daily_spend_micros'], result['currency'] = None, None
        else:
            if (not isinstance(money, dict) or not isinstance(money.get('currency'), str)
                    or re.fullmatch(r'[A-Z]{3}', money['currency']) is None):
                raise AccountsError('Write the daily spending limit as an amount, such as 25.00.')
            try:
                micros = parse_amount(money.get('amount'), whole_digits=7)
            except InvalidRateCard:
                raise AccountsError('Write the daily spending limit as an amount, such as 25.00.') from None
            result['daily_spend_micros'], result['currency'] = (micros or None), (money['currency'] if micros else None)
    else:
        result['daily_spend_micros'] = current.get('daily_spend_micros')
        result['currency'] = current.get('currency')
    return {name: value for name, value in result.items() if value is not None}


def _typed(value_field, value, label):
    kind = _kind(value_field)
    if value is None or value == '':
        return None
    if kind is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ('true', 'yes', 'on', '1', 'false', 'no', 'off', '0'):
            return value.strip().lower() in ('true', 'yes', 'on', '1')
        raise AccountsError(f'{label} is a yes or no setting.')
    if kind is int:
        if type(value) is int:
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
        raise AccountsError(f'{label} must be a whole number.')
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise AccountsError(f'{label} must be text.')
    text = str(value).strip()
    if len(text) > 1024:
        raise AccountsError(f'{label} is too long.')
    return text


def _clean_fields(provider, settings, credentials, current_settings=None, current_credentials=None):
    fields = {name: (value_field, label) for name, value_field, label, *_ in FIELDS.get(provider, ())}
    settings = {} if settings is None else settings
    credentials = {} if credentials is None else credentials
    if not isinstance(settings, dict) or not isinstance(credentials, dict):
        raise AccountsError('Settings and credentials must each be a list of names and values.')
    clean_settings = dict(current_settings or {})
    clean_credentials = dict(current_credentials or {})
    for source, given_as_secret in ((settings, False), (credentials, True)):
        for name, value in source.items():
            if name not in fields:
                known = ', '.join(sorted(fields)) or 'none'
                raise AccountsError(f'This provider has no setting called {str(name)[:40]}. Its settings are: {known}.')
            value_field, label = fields[name]
            secret = is_secret(value_field)
            if secret and not given_as_secret:
                # A secret among the settings would be shown back on the account list.
                raise AccountsError(f'{label} is a secret, so it goes with the credentials.')
            # A plain setting sent with the credentials is kept as a setting.
            if secret and isinstance(value, str) and re.fullmatch(r'\*+[\s\S]{0,4}', value):
                raise AccountsError(f'Enter {label} itself; Faxbot never shows a saved secret, so a masked value '
                                    'cannot be saved.')
            typed = _typed(value_field, value, label)
            target = clean_credentials if secret else clean_settings
            if typed is None:
                target.pop(name, None)
            else:
                target[name] = typed
    return clean_settings, clean_credentials


def _check_numbers(values, docs, candidate_key, numbers, provider):
    """Each number belongs to one receiving account; a second claim is refused."""
    if not numbers:
        return
    trial = values.with_provider_accounts(_document(docs))
    for account in all_accounts(trial):
        if account.key == candidate_key or not account.receives:
            continue
        clash = sorted(set(account.numbers) & set(numbers))
        if clash:
            raise AccountsError(f'{clash[0]} already receives on {account.label}. A number belongs to one receiving '
                                'account; take it off that account first.', 409)


def _document(docs):
    from .config_profiles import ConfigurationDocument
    return ConfigurationDocument(docs)


def _check_site(sites, site):
    if site is None:
        return None
    if not isinstance(site, str) or not site.strip():
        return None
    site = site.strip()
    if sites is not None and site not in sites:
        raise AccountsError(f"There is no site called {site[:40]}. Add it on Delivery setup → Routing rules first.")
    return site


def _check_label(label, fallback):
    if label is None:
        return fallback
    if not isinstance(label, str) or not label.strip() or len(label.strip()) > 100:
        raise AccountsError('Give the account a name of up to 100 characters.')
    return label.strip()


def added(values, body, *, provider_ids, sites=None):
    """The accounts document with one extra account added from the API's AccountInput. Raises AccountsError."""
    docs = dict(documents(values))
    key = body.get('key')
    if not isinstance(key, str) or KEY.fullmatch(key) is None:
        raise AccountsError('Choose a key of up to 32 lowercase letters, digits, - or _, starting with a letter or '
                            'digit, such as sinch-uk.')
    if key in RESERVED or key in provider_ids or key in PROVIDER_ORDER:
        raise AccountsError(f'{key} is kept for Faxbot itself or for the first account of a provider. Choose another '
                            'key, such as ' + f'{key}-2.')
    if key in docs or account_named(values, key) is not None:
        raise AccountsError(f'There is already an account called {key}. Choose another key.', 409)
    provider = body.get('provider')
    if provider not in FIELDS or provider not in provider_ids:
        raise AccountsError('Faxbot can add a second account only for these providers: '
                            + ', '.join(sorted(name for name in FIELDS if name in provider_ids)) + '.')
    receives = bool(body.get('receives'))
    if receives and not supports_inbound(provider):
        raise AccountsError(f'{_sentence_label(values, provider)} cannot receive faxes, so turn receiving off.')
    sends = body.get('sends', True) is not False
    if not sends and not receives:
        raise AccountsError('An account has to send faxes, receive them, or both.')
    settings, credentials = _clean_fields(provider, body.get('settings'), body.get('credentials'))
    numbers = _clean_number_list(values, body.get('numbers'))
    doc = {'provider': provider, 'label': _check_label(body.get('label'), f'{provider_name(provider, values)} ({key})'),
           'site': _check_site(sites, body.get('site')), 'sends': sends, 'receives': receives, 'enabled': True,
           'numbers': numbers, 'settings': settings, 'credentials': credentials,
           'limits': _clean_limits(values, body.get('limits'), provider)}
    _check_numbers(values, docs, key, numbers if receives else [], provider)
    docs[key] = {name: value for name, value in doc.items() if value is not None}
    _validate_values(values, key, docs[key])
    return docs


def _validate_values(values, key, doc):
    """Run the provider's own field checks on an extra account's settings; raises AccountsError."""
    try:
        values.with_patch(_patch_for(doc['provider'], doc))
    except ValueError as error:
        raise AccountsError(_value_sentence(doc['provider'], error)) from None


def patched(values, key, body, *, provider_ids, sites=None):
    """(accounts document, configuration changes) after one AccountPatch. Raises AccountsError.

    A primary account takes only what the account list owns (on or off, site, label, daily limit, and numbers
    for a provider other than the trunk); its provider settings are changed on its own provider page.
    """
    docs = dict(documents(values))
    account = account_named(values, key)
    if account is None:
        raise AccountsError('Faxbot has no account with this key.', 404)
    changes = {}
    current = dict(docs.get(key) or {})
    name = account.label
    if body.get('enabled') is False and account.default_sending:
        raise AccountsError(f'{name} is the default sending account. Make another account the default first.', 409)
    if account.primary:
        refused = [field for field in ('settings', 'credentials', 'sends', 'receives') if body.get(field) is not None]
        if refused:
            raise AccountsError(f"Change the first {provider_name(account.provider, values)} account's settings on its "
                                'own provider page.')
        if account.provider == 'sip' and body.get('numbers') is not None:
            raise AccountsError("Change the trunk's numbers on its own page.")
        if account.provider == 'sip' and isinstance(body.get('limits'), dict) and any(
                body['limits'].get(item) for item in ('at_once', 'calls_per_second')):
            if (body['limits'].get('at_once'), body['limits'].get('calls_per_second')) != (
                    account.at_once, account.calls_per_second):
                raise AccountsError("Change the trunk's lines and calls a second on its own page.")
    if 'label' in body and body['label'] is not None:
        current['label'] = _check_label(body['label'], name)
    if 'site' in body:
        site = _check_site(sites, body['site'])
        if site is None:
            current.pop('site', None)
        else:
            current['site'] = site
    if body.get('enabled') is not None:
        current['enabled'] = bool(body['enabled'])
    if body.get('limits') is not None:
        limits = _clean_limits(values, body['limits'], account.provider, current.get('limits'))
        if account.primary and account.provider == 'sip':
            limits.pop('at_once', None), limits.pop('calls_per_second', None)
        if limits:
            current['limits'] = limits
        else:
            current.pop('limits', None)
    if not account.primary:
        if body.get('sends') is not None:
            current['sends'] = bool(body['sends'])
        if body.get('receives') is not None:
            if body['receives'] and not supports_inbound(account.provider):
                raise AccountsError(f'{provider_name(account.provider, values)} cannot receive faxes.')
            current['receives'] = bool(body['receives'])
        if body.get('settings') is not None or body.get('credentials') is not None:
            settings, credentials = _clean_fields(account.provider, body.get('settings'), body.get('credentials'),
                                                  current.get('settings'), current.get('credentials'))
            current['settings'], current['credentials'] = settings, credentials
        if current.get('sends') is False and not current.get('receives'):
            raise AccountsError('An account has to send faxes, receive them, or both.')
        if current.get('sends') is False and account.default_sending:
            raise AccountsError(f'{name} is the default sending account. Make another account the default first.', 409)
    if body.get('numbers') is not None:
        current['numbers'] = _clean_number_list(values, body['numbers'])
    receives_now = current.get('receives') if not account.primary else account.receives
    if current.get('numbers') and (receives_now or account.primary):
        _check_numbers(values, docs, key, current['numbers'], account.provider)
    if body.get('default_sending'):
        changes.update(_make_default(values, docs, account, current, 'sending'))
    if body.get('default_receiving'):
        changes.update(_make_default(values, docs, account, current, 'receiving'))
    if not current.get('numbers'):
        current.pop('numbers', None)
    if account.primary:
        overlay = {name: value for name, value in current.items() if name in ('label', 'site', 'enabled', 'limits',
                                                                               'numbers')}
        if overlay.get('enabled') is True:
            overlay.pop('enabled')
        if overlay.get('label') == provider_name(account.provider, values):
            overlay.pop('label')
        if overlay:
            docs[key] = overlay
        else:
            docs.pop(key, None)
    else:
        docs[key] = current
        _validate_values(values, key, current)
    return docs, changes


def _make_default(values, docs, account, current, role):
    """Make one account the default for sending or receiving; returns the configuration changes it needs."""
    flag, field_name = ('default_sending', 'outbound_backend') if role == 'sending' else (
        'default_receiving', 'inbound_backend')
    sends = account.sends if account.primary else current.get('sends', True) is not False
    receives = account.receives if account.primary else bool(current.get('receives'))
    if role == 'sending' and not sends:
        raise AccountsError(f'{account.label} does not send faxes. Turn sending on first.')
    if role == 'receiving' and not receives:
        raise AccountsError(f'{account.label} does not receive faxes. Turn receiving on first.')
    if current.get('enabled', True) is False:
        raise AccountsError(f'{account.label} is off. Turn it on first.')
    for key, doc in docs.items():
        if _is_extra(key, doc) and doc.get(flag) and key != account.key:
            docs[key] = {name: value for name, value in doc.items() if name != flag}
    if account.primary:
        current.pop(flag, None)
        return {field_name: account.provider}
    # Routes, route choices and cost records are keyed by account (WP-C), so an extra account may be the default for
    # sending beside its provider's first account: each keeps its own route, rate card and costs.
    current[flag] = True
    return {field_name: account.provider}


def provider_kinds(values, provider_ids):
    """What each provider an extra account can be added for asks for (the API's ProviderKind list)."""
    kinds = []
    for provider in FIELDS:
        if provider not in provider_ids:
            continue
        kinds.append({'id': provider, 'label': provider_name(provider, values) if provider != 'sip' else 'Carrier trunk',
                      'supports_inbound': supports_inbound(provider),
                      'fields': [{'name': name, 'label': label, 'secret': is_secret(value_field), 'required': required,
                                  'help': help_text}
                                 for name, value_field, label, required, help_text in FIELDS[provider]]})
    return kinds
