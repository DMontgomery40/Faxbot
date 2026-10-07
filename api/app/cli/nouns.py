"""The shape of the faxbot command: two verbs, then the console's eight areas.

`faxbot send` and `faxbot status`, then received, sent, numbers, recipients,
providers, costs, access and system, each holding what its console area holds.
Commands are defined in their modules; this module gives each one its home.
"""
import typer

from .commands import access, admin, delivery, fax, operations, pages, settings, setup, sslfax, trunk, work

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
recipients.command('limits')(sslfax.recipient_limits)
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
recipients.add_typer(delivery.toll_free, name='toll-free')

# -- providers -----------------------------------------------------------------------

providers = _group('The fax services Faxbot sends and receives with, their settings, and your own phone line for faxing.')
providers.command('list')(settings.providers_list)
providers.command('status')(settings.providers_status)
providers.command('show')(settings.providers_config)
providers.command('configure')(settings.providers_configure)
providers.command('callbacks')(settings.providers_callbacks)
providers.command('validate')(settings.providers_validate)
providers.command('install')(settings.providers_install)
providers.command('import')(settings.providers_import)
providers.command('long-pages')(pages.providers_long_pages)
efax = _group('eFax receiving: whether Faxbot is collecting your faxes from eFax.')
efax.command('status')(settings.efax_status)
providers.add_typer(efax, name='efax')
humblefax = _group('HumbleFax receiving: whether Faxbot is collecting your faxes from HumbleFax, and checking now.')
humblefax.command('status')(settings.humblefax_status)
humblefax.command('check')(settings.humblefax_check)
providers.add_typer(humblefax, name='humblefax')
providers.add_typer(trunk.trunk, name='trunk')

# -- costs ---------------------------------------------------------------------------

costs = _group('What faxing costs you: spending by route, charges from your carrier, and the prices and plans Faxbot'
               ' uses.')
costs.command('spending')(delivery.routing_costs)
costs.command('reconcile')(delivery.routing_reconcile)
costs.command('fax')(delivery.routing_fax_cost)
costs.command('received')(delivery.routing_received_costs)
costs.command('savings')(delivery.routing_savings)
costs.add_typer(delivery.recommendations, name='recommendations')
costs.command('rate-cards')(delivery.routing_rate_cards)
costs.command('plans')(delivery.routing_plans)
costs.command('predict')(delivery.routing_predict)

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

# -- system --------------------------------------------------------------------------

system = _group('Look after the installation: settings, checks, logs, the security log, backups and restarts.')
system.add_typer(settings.settings, name='settings')
checks = _copy(settings.diagnostics)
checks.command('test-fax')(fax.inbound_simulate)
system.add_typer(checks, name='diagnostics')
system.command('health')(settings.health)
system.add_typer(operations.logs, name='logs')
system.command('audit')(access.audit_list)
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


def register(app):
    app.command('send')(fax.send)
    app.command('status')(fax.status)
    for name, home in HOMES.items():
        app.add_typer(home, name=name)
