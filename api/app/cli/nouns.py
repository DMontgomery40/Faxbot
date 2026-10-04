"""The shape of the faxbot command: two verbs, then the console's eight areas.

`faxbot send` and `faxbot status`, then received, sent, numbers, recipients,
providers, costs, access and system, each holding what its console area holds.
Commands are defined in their modules under their older groups; this module
gives each one its home. The older groups stay registered but hidden, so
scripts written for them keep working. MOVED lists every older command path
with its home; the reference page and the tests are generated from it.
"""
import typer

from .commands import access, admin, delivery, fax, operations, settings, setup, trunk, work

NOUNS = ('received', 'sent', 'numbers', 'recipients', 'providers', 'costs', 'access', 'system')


def _group(help):
    return typer.Typer(help=help, no_args_is_help=True)


def _copy(source, *, help=None, rename=None):
    """A new group holding an older group's commands and groups, some under new names."""
    rename = rename or {}
    target = _group(help or source.info.help)
    for info in source.registered_commands:
        target.command(rename.get(info.name, info.name), help=info.help)(info.callback)
    for info in source.registered_groups:
        target.add_typer(info.typer_instance, name=rename.get(info.name, info.name))
    return target


# -- received ------------------------------------------------------------------------

received = _group('Received faxes: list and open them, give each one an owner, and see where they were delivered.')
received.command('list')(fax.inbound_list)
received.command('show')(fax.inbound_get)
received.command('pdf')(fax.inbound_pdf)
received.command('fetch')(fax.inbound_fetch)
received.command('recover')(fax.inbound_recover)
received.command('import')(work.import_document)
received.command('owners')(work.work_list)
received.command('history')(work.work_show)
received.command('counts')(work.received_counts)
received.command('assign')(work.work_assign)
received.command('acknowledge')(work.work_acknowledge)
received.command('done')(work.work_done)
received.command('reopen')(work.work_reopen)
received.command('export')(work.work_export)
deliveries = _group('Delivery of received faxes to email and other places, and any that failed.')
deliveries.command('list')(delivery.intake_items)
deliveries.command('retry')(delivery.intake_retry)
received.add_typer(deliveries, name='deliveries')

# -- sent ----------------------------------------------------------------------------

sent = _group('Sent faxes: list them, open one, download what was sent and follow its delivery.')
sent.command('list')(fax.jobs_list)
sent.command('show')(fax.jobs_get)
sent.command('pdf')(fax.jobs_pdf)
sent.command('refresh')(fax.jobs_refresh)
sent.command('evidence')(fax.jobs_history)
sent.command('confirm-receipt')(fax.jobs_reconcile)
sent.command('history', hidden=True)(fax.jobs_history)
sent.command('reconcile', hidden=True)(fax.jobs_reconcile)
sent.command('send-now')(fax.jobs_send_now)

# -- numbers -------------------------------------------------------------------------

numbers = _group("Your fax numbers: which mailbox each number's faxes go to, the mailboxes themselves, and email "
                 'delivery.')
numbers.command('list')(access.numbers_list)
numbers.command('add')(access.numbers_add)
numbers.command('update')(access.numbers_update)
mailboxes = _group('Mailboxes that hold received faxes, and how soon someone should acknowledge them.')
mailboxes.command('list')(access.mailboxes_list)
mailboxes.command('add')(access.mailboxes_add)
mailboxes.command('update')(access.mailboxes_update)
mailboxes.command('target')(work.work_settings)
numbers.add_typer(mailboxes, name='mailboxes')
email = _group('Email delivery of received faxes.')
email.add_typer(delivery.connectors, name='connectors')
numbers.add_typer(email, name='email')

# -- recipients ----------------------------------------------------------------------

recipients = _group('Fax numbers you send to: routing, batching several faxes into one call, direct delivery partners and'
                    ' case packets.')
recipients.command('list')(delivery.routing_destinations)
recipients.command('show')(delivery.routing_destination)
recipients.command('set')(delivery.routing_update_destination)
recipients.add_typer(delivery.batching, name='together')
partners = _group('Partners: other offices running Faxbot, which get your faxes over the internet instead of a phone '
                  'call.')
partners.command('card')(delivery.direct_card)
partners.command('list')(delivery.peers_list)
partners.command('add')(delivery.peers_add)
partners.command('challenge')(delivery.peers_challenge)
partners.command('confirm')(delivery.peers_confirm)
partners.command('revoke')(delivery.peers_revoke)
partners.command('deliveries')(delivery.direct_deliveries)
recipients.add_typer(partners, name='partners')
recipients.add_typer(delivery.cases, name='cases')

# -- providers -----------------------------------------------------------------------

providers = _group('The fax services Faxbot sends and receives with, their settings, and your own phone line for faxing.')
providers.command('list')(settings.providers_list)
providers.command('status')(settings.providers_status)
providers.command('show')(settings.providers_config)
providers.command('config', hidden=True)(settings.providers_config)
providers.command('configure')(settings.providers_configure)
providers.command('callbacks')(settings.providers_callbacks)
providers.command('validate')(settings.providers_validate)
providers.command('install')(settings.providers_install)
registry = typer.Typer(help='Fax services you can add from the provider list, one at a time or several at once.',
                       invoke_without_command=True)


@registry.callback()
def _registry(ctx: typer.Context):
    # The older `faxbot providers registry`, with nothing after it, still lists them.
    if ctx.invoked_subcommand is None:
        settings.providers_registry()


registry.command('list')(settings.providers_registry)
registry.command('import')(settings.providers_import)
providers.add_typer(registry, name='registry')
efax = _group('eFax receiving: whether Faxbot is collecting your faxes from eFax.')
efax.command('status')(settings.efax_status)
providers.add_typer(efax, name='efax')
providers.add_typer(trunk.trunk, name='trunk')

# -- costs ---------------------------------------------------------------------------

costs = _group('What faxing costs you: spending by route, charges from your carrier, and the prices and plans Faxbot'
               ' uses.')
costs.command('spending')(delivery.routing_costs)
costs.command('reconcile')(delivery.routing_reconcile)
costs.command('fax')(delivery.routing_fax_cost)
costs.command('received')(delivery.routing_received_costs)
costs.command('savings')(delivery.routing_savings)
costs.command('rate-cards')(delivery.routing_rate_cards)
costs.command('plans')(delivery.routing_plans)

# -- access --------------------------------------------------------------------------

people = _group('Who can use Faxbot and what each person may do: people, groups, roles, keys, sign-ins and paired '
                'phones.')
people.command('me')(access.me)
people.add_typer(_copy(access.users, rename={'get': 'show'}), name='users')
people.add_typer(access.integrations, name='integrations')
people.add_typer(_copy(access.groups, rename={'get': 'show'}), name='groups')
people.add_typer(_copy(access.roles, rename={'get': 'show'}), name='roles')
grants = _group('Who has which role, and for which mailboxes or faxes.')
grants.command('list')(access.access_list)
grants.command('add')(access.access_grant)
grants.command('remove')(access.access_revoke)
people.add_typer(grants, name='grants')
people.add_typer(access.keys, name='keys')
people.add_typer(access.sessions, name='sessions')
people.add_typer(settings.pair, name='pair')
people.add_typer(access.resources, name='resources')
people.add_typer(access.owner, name='owner')
# The older `faxbot access list|grant|revoke` share this group's name.
people.command('list', hidden=True)(access.access_list)
people.command('grant', hidden=True)(access.access_grant)
people.command('revoke', hidden=True)(access.access_revoke)

# -- system --------------------------------------------------------------------------

system = _group('Look after the installation: settings, checks, logs, remote access, the security log, backups and '
                'restarts.')
system.add_typer(settings.settings, name='settings')
checks = _copy(settings.diagnostics)
checks.command('test-fax')(fax.inbound_simulate)
system.add_typer(checks, name='diagnostics')
system.command('health')(settings.health)
system.add_typer(operations.logs, name='logs')
system.command('audit')(access.audit_list)
system.add_typer(operations.tunnel, name='tunnel')
system.add_typer(operations.actions, name='actions')
system.command('restart')(operations.restart)
profiles = _copy(setup.config, help='Server addresses and keys saved on this computer, so you do not have to type them each time.',
                 rename={'set-profile': 'save', 'show': 'list'})
system.add_typer(profiles, name='profiles')
# On a stopped installation on this computer.
for _name, _command in (('status', admin.admin_status), ('migrate', admin.admin_migrate),
                        ('recover-owner', admin.admin_recover_owner), ('backup', admin.admin_backup),
                        ('restore', admin.admin_restore)):
    system.command(_name)(admin.on_this_computer(_command))

HOMES = {'received': received, 'sent': sent, 'numbers': numbers, 'recipients': recipients, 'providers': providers,
         'costs': costs, 'access': people, 'system': system}

# Older groups and commands, hidden from help. `numbers`, `providers` and `access` keep their name.
OLDER_GROUPS = {
    'jobs': fax.jobs, 'inbound': fax.inbound, 'work': work.work, 'routing': delivery.routing,
    'intake': delivery.intake, 'direct': delivery.direct, 'cases': delivery.cases, 'trunk': trunk.trunk,
    'settings': settings.settings, 'diagnostics': settings.diagnostics, 'pair': settings.pair,
    'logs': operations.logs, 'tunnel': operations.tunnel, 'actions': operations.actions, 'config': setup.config,
    'admin': admin.admin, 'owner': access.owner, 'users': access.users, 'integrations': access.integrations,
    'groups': access.groups, 'roles': access.roles, 'resources': access.resources, 'keys': access.keys,
    'sessions': access.sessions, 'mailboxes': access.mailboxes, 'audit': access.audit,
}
OLDER_COMMANDS = {'me': access.me, 'health': settings.health, 'restart': operations.restart,
                  'import': work.import_document}


def _moved():
    """Every older command path and its home, as tuples of words."""
    moved = {}

    def under(old, new, names, rename=None):
        for name in names:
            moved[(*old, name)] = (*new, (rename or {}).get(name, name))

    under(('jobs',), ('sent',), ('list', 'pdf', 'refresh', 'history', 'reconcile', 'send-now', 'get'),
          {'get': 'show', 'history': 'evidence', 'reconcile': 'confirm-receipt'})
    under(('inbound',), ('received',), ('list', 'get', 'pdf', 'fetch', 'recover'), {'get': 'show'})
    moved[('inbound', 'simulate')] = ('system', 'diagnostics', 'test-fax')
    under(('work',), ('received',), ('list', 'show', 'assign', 'acknowledge', 'done', 'reopen', 'export'),
          {'list': 'owners', 'show': 'history'})
    moved[('work', 'settings')] = ('numbers', 'mailboxes', 'target')
    moved[('import',)] = ('received', 'import')
    under(('routing',), ('recipients',), ('destinations', 'destination', 'update-destination'),
          {'destinations': 'list', 'destination': 'show', 'update-destination': 'set'})
    under(('routing',), ('costs',), ('costs', 'reconcile', 'fax-cost', 'rate-cards', 'plans'),
          {'costs': 'spending', 'fax-cost': 'fax'})
    under(('routing', 'batching'), ('recipients', 'together'), ('show', 'set', 'off'))
    under(('intake',), ('received', 'deliveries'), ('items', 'retry'), {'items': 'list'})
    under(('intake', 'connectors'), ('numbers', 'email', 'connectors'), ('list', 'add', 'update', 'test', 'remove'))
    under(('direct',), ('recipients', 'partners'), ('card', 'deliveries'))
    under(('direct', 'peers'), ('recipients', 'partners'), ('list', 'add', 'challenge', 'confirm', 'revoke'))
    under(('cases',), ('recipients', 'cases'), ('documents', 'send'))
    under(('trunk',), ('providers', 'trunk'), ('status', 'apply', 'calls', 'mode', 'presets', 'use'))
    under(('settings',), ('system', 'settings'), ('get', 'set', 'validate', 'persist', 'export'))
    moved[('providers', 'config')] = ('providers', 'show')
    moved[('providers', 'registry')] = ('providers', 'registry', 'list')
    moved[('health',)] = ('system', 'health')
    under(('diagnostics',), ('system', 'diagnostics'), ('run', 'database'))
    under(('pair',), ('access', 'pair'), ('new', 'device'))
    under(('logs',), ('system', 'logs'), ('list', 'tail'))
    under(('tunnel',), ('system', 'tunnel'), ('status', 'set', 'test'))
    under(('actions',), ('system', 'actions'), ('list', 'run'))
    moved[('restart',)] = ('system', 'restart')
    under(('config',), ('system', 'profiles'), ('set-profile', 'show', 'use', 'remove'),
          {'set-profile': 'save', 'show': 'list'})
    under(('admin',), ('system',), ('status', 'migrate', 'recover-owner', 'backup', 'restore'))
    moved[('me',)] = ('access', 'me')
    moved[('owner', 'enroll')] = ('access', 'owner', 'enroll')
    under(('users',), ('access', 'users'), ('list', 'get', 'add', 'update', 'reset-password'), {'get': 'show'})
    under(('integrations',), ('access', 'integrations'), ('add', 'list'))
    under(('groups',), ('access', 'groups'), ('list', 'get', 'add', 'update'), {'get': 'show'})
    under(('groups', 'members'), ('access', 'groups', 'members'), ('add', 'remove'))
    under(('roles',), ('access', 'roles'), ('list', 'get', 'permissions', 'add', 'update'), {'get': 'show'})
    under(('access',), ('access', 'grants'), ('list', 'grant', 'revoke'), {'grant': 'add', 'revoke': 'remove'})
    moved[('resources', 'list')] = ('access', 'resources', 'list')
    under(('keys',), ('access', 'keys'), ('list', 'create', 'update', 'rotate', 'revoke', 'approve'))
    under(('sessions',), ('access', 'sessions'), ('list', 'revoke'))
    under(('mailboxes',), ('numbers', 'mailboxes'), ('list', 'add', 'update'))
    moved[('audit', 'list')] = ('system', 'audit')
    return moved


MOVED = _moved()


def register(app):
    app.command('send')(fax.send)
    app.command('status')(fax.status)
    for name, home in HOMES.items():
        app.add_typer(home, name=name)
    for name, group in OLDER_GROUPS.items():
        app.add_typer(group, name=name, hidden=True)
    for name, command in OLDER_COMMANDS.items():
        app.command(name, hidden=True)(command)
