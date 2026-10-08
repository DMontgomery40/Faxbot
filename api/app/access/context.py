"""Closed console hints from current authority and one active configuration.

These hints permit navigation only. Every later operation still requires its
own fresh policy decision; no secret or desired editor configuration leaves
this boundary.
"""
from datetime import datetime
from urllib.parse import urlsplit

import sqlalchemy as sa

from ..config_store import ConfigurationNotInitialized, ConfigurationStoreError
from ..provider_labels import trunk_name
from ..routing.numbers import number_example
from .types import InvalidTransactionError, ResourceRef


def _documentation_url(value):
    # A base link has no query/fragment credential channel. Do not normalize
    # untrusted characters into a different destination, or fetch the URL.
    valid = (type(value) is str and 0 < len(value) <= 2048
             and all(33 <= ord(character) < 127 for character in value)
             and not any(character in value for character in '\\<>"\''))
    if valid:
        try:
            parts = urlsplit(value)
            valid = (parts.scheme in {'http', 'https'} and bool(parts.hostname)
                     and parts.username is None and parts.password is None
                     and not parts.query and not parts.fragment
                     and parts.port != 0)
        except ValueError:
            valid = False
    if not valid:
        raise ConfigurationStoreError('Invalid console documentation URL.')
    return value


class ConsoleContext:
    def __init__(self, configuration, control, *, clock=None,
                 docs_base='https://docs.faxbot.net/latest/'):
        self.configuration = configuration
        self.control = control
        self.clock = datetime.utcnow if clock is None else clock
        self.docs_base = _documentation_url(docs_base)
        self._require_pairing()

    def _require_pairing(self):
        if getattr(self.control, 'store', None) is not self.configuration.access_store:
            raise InvalidTransactionError()

    def _has_scope_on(self, connection, actor, source, permission, kinds):
        resource, parent, tree, valid = self.control._resource_query()
        query = sa.select(resource.c.id).select_from(tree).where(
            valid, resource.c.kind.in_(kinds),
            self.control._authority_predicate(actor, source, permission, resource, parent))
        return bool(connection.execute(sa.select(query.exists())).scalar_one())

    def snapshot(self, actor):
        self._require_pairing()
        configuration, control = self.configuration, self.control
        with configuration._locked() as connection:
            configuration._require_lock_on(connection)
            configuration.access_store.lock_on(connection)
            # A clock sampled before either lock could outlive credential expiry
            # while waiting. Identity and all hints are read after both locks.
            now = self.clock()
            source = control._current_source_on(connection, actor, now)
            policy_version = configuration.access_store.require_lock_on(connection)
            head = configuration._head(connection)
            if head is None:
                raise ConfigurationNotInitialized('Configuration has not been initialized.')
            active = configuration._revision(connection, configuration._cipher(),
                head['installation_id'], head['active_revision_id'])

            permissions = set()
            jobs, inbox, send = False, False, False
            if not source.reset_required:
                permissions.update(control.effective_access_on(connection, actor,
                    ResourceRef('installation'), now=now))
                resources = control.tables['access_resources']
                personal = connection.execute(sa.select(resources.c.id).where(
                    resources.c.kind == 'personal', resources.c.principal_id == actor.principal_id)).scalar_one_or_none()
                if personal is not None:
                    send = control.authorize_on(connection, actor, 'fax:send',
                        ResourceRef(personal), now=now).allowed
                if send:
                    permissions.add('fax:send')
                jobs = self._has_scope_on(connection, actor, source, 'fax:read',
                    ('personal', 'legacy', 'outbound'))
                inbox = self._has_scope_on(connection, actor, source, 'inbound:list',
                    ('mailbox', 'legacy', 'inbound'))

            values = active.values
            send_choices = {}
            if send:
                # What sending rules can match on Send a fax: mailboxes this person may send from, and the
                # organization's workflows and labels (routing/rules_acceptance.py). Offered only when there are some.
                try:
                    from ..routing import rules_acceptance
                    send_choices = {'mailboxes': rules_acceptance.sendable_mailboxes(connection, control, actor,
                                                                                     now=now),
                                    **rules_acceptance.send_choices(connection=connection)}
                except Exception:
                    send_choices = {}
                # Offered only when the organization uses them, so the context is unchanged until then.
                send_choices = {name: value for name, value in send_choices.items() if value}
            return {
                'policy_version': policy_version,
                'active_revision_id': active.id,
                'generation': head['generation'],
                'permissions': sorted(permissions),
                'navigation': {'jobs': jobs, 'inbox': inbox, 'send': send},
                'send': {'fax_disabled': values.fax_disabled,
                         'max_file_size_mb': values.max_file_size_mb,
                         'default_country': values.fax_default_country,
                         'number_example': number_example(values.fax_default_country)['national'],
                         **send_choices,
                         } if send else None,
                'inbound_enabled': values.inbound_enabled if inbox else None,
                'branding': {'docs_base': values.docs_base_url, 'logo_path': '/admin/ui/faxbot_full_logo.png'},
                'provider_view': {
                    'plugins_enabled': values.feature_v3_plugins,
                    'install_enabled': values.feature_plugin_install,
                    'active_outbound': values.effective_outbound,
                    'active_inbound': values.effective_inbound,
                    'extra_routes': [route for route in values.outbound_route_providers if route != values.effective_outbound],
                    'trunk_preset': values.sip_trunk_preset,
                } if 'providers:read' in permissions else None,
                # The trunk is shown by its carrier's name on every screen that names a provider.
                'provider_names': {'sip': trunk_name(values.sip_trunk_preset)},
            }
