"""The shape of the faxbot command: two verbs, then the console's eight areas.

`faxbot send` and `faxbot status`, then received, sent, numbers, recipients,
providers, costs, access and system, each holding what its console area holds.
`faxbot forms` is the Faxes area's Forms page, beside send, received and sent; `faxbot expected` is its
Expected page.
Commands are defined in their modules; this module gives each one its home.
"""
import typer

from .commands import (access, accounts, admin, blocked, codec, connectors, delivery, fax, fax_machines, forms,
                       notices, operations, pages, relay, reply, rules, schedule, settings, setup, sslfax, trunk, work)
from .commands import polling as polling_commands
from .commands import certainty, continuation, discovery
from .commands import send_once
from .commands import charges as charge_commands
from .commands import setup_plan
from .commands import number_advice
from .commands import digital
from .commands import forwarded_trust
from .commands import expected as expected_commands
from .commands import cases as case_commands

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
received.command('decoded')(codec.received_decoded)
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
received.command('block')(blocked.received_block)
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
sent.command('route')(rules.route_command)
sent.command('approve')(rules.approve_command)
sent.command('refuse')(rules.refuse_command)
sent.command('check-again')(rules.check_again_command)
# Sent faxes Faxbot could not confirm: their owner, the checks ranked by cost, and settling them.
sent.command('uncertain')(certainty.uncertain_list)
sent.command('probe')(certainty.uncertain_probe)
sent.command('settle')(certainty.uncertain_settle)
sent.command('assign')(certainty.uncertain_assign)
sent.command('uncertain-settings')(certainty.uncertain_settings)
# A fax whose call broke part way: send only the pages the receiving machine did not confirm.
sent.command('continue')(continuation.continue_fax)

# -- numbers -------------------------------------------------------------------------

numbers = _group("Your fax numbers: which mailbox each number's faxes go to, the mailboxes themselves, email "
                 'delivery, and the mailboxes and folders that bring documents in or send faxes.')
numbers.command('list')(access.numbers_list)
numbers.command('add')(access.numbers_add)
numbers.command('update')(access.numbers_update)
numbers.command('explain')(rules.numbers_explain)
mailboxes = _group('Mailboxes that hold received faxes, and how soon someone should acknowledge them.')
mailboxes.command('list')(access.mailboxes_list)
mailboxes.command('add')(access.mailboxes_add)
mailboxes.command('update')(access.mailboxes_update)
mailboxes.command('target')(work.work_settings)
numbers.add_typer(mailboxes, name='mailboxes')
email = _group('Email delivery of received faxes.')
email.add_typer(delivery.connectors, name='connectors')
numbers.add_typer(email, name='email')
numbers.add_typer(reply.reply, name='reply')
numbers.add_typer(blocked.blocked, name='blocked')
numbers.add_typer(connectors.connectors, name='connectors')
numbers.add_typer(number_advice.npi, name='npi')
numbers.add_typer(forwarded_trust.forwarded_trust, name='forwarded-trust')

# -- recipients ----------------------------------------------------------------------

recipients = _group('Fax numbers you send to: routing, batching several faxes into one call, direct delivery partners and'
                    ' case packets.')
recipients.command('list')(delivery.routing_destinations)
recipients.command('show')(delivery.routing_destination)
recipients.command('set')(delivery.routing_update_destination)
recipients.command('limits')(sslfax.recipient_limits)
recipients.command('fax-machine')(fax_machines.fax_machine)
recipients.add_typer(fax_machines.iaf, name='iaf')
recipients.command('schedule')(schedule.recipient_schedule)
recipients.command('polling')(polling_commands.recipient_polling)
recipients.command('collect')(polling_commands.recipient_collect)
recipients.add_typer(delivery.batching, name='together')
recipients.add_typer(codec.numbers, name='encoded')
partners = _group('Partners: other offices running Faxbot, which get your faxes over the internet instead of a phone '
                  'call.')
partners.command('card')(delivery.direct_card)
partners.command('list')(delivery.peers_list)
partners.command('add')(delivery.peers_add)
partners.command('challenge')(delivery.peers_challenge)
partners.command('confirm')(delivery.peers_confirm)
partners.command('revoke')(delivery.peers_revoke)
partners.command('fax-images')(delivery.peers_fax_images)
partners.command('tunnel-calls')(delivery.peers_tunnel_calls)
partners.command('tunnel-check')(delivery.peers_tunnel_check)
partners.command('deliveries')(delivery.direct_deliveries)
partners.command('notice-fax')(notices.notice_fax)
partners.command('notices')(notices.notices_list)
partners.command('notice-faxes')(notices.notice_faxes)
partners.command('pair')(notices.notice_pair)
partners.command('transfers')(notices.transfers_list)
partners.command('repairs')(notices.repairs_list)
partners.add_typer(relay.relay, name='relay')
partners.add_typer(send_once.send_once, name='send-once')
# Find partners: suggestions from calls, introductions and trusted directories, and publishing your number.
partners.add_typer(discovery.discover, name='discover')
partners.command('introduce')(discovery.introduce)
partners.command('may-introduce')(discovery.may_introduce)
partners.add_typer(discovery.publish, name='publish')
recipients.add_typer(partners, name='partners')
# The case group, with the commands case_commands adds (accept, repair, checklists ...).
recipients.add_typer(case_commands.cases, name='cases')
recipients.add_typer(delivery.toll_free, name='toll-free')
recipients.command('check')(number_advice.recipient_check)
recipients.add_typer(digital.recipients, name='digital')

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
providers.add_typer(accounts.accounts, name='accounts')
providers.add_typer(digital.accounts, name='digital')
providers.add_typer(rules.rules, name='rules')

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
costs.command('rate-rows')(delivery.routing_rate_rows)
costs.add_typer(delivery.plans, name='plans')
costs.command('state-prices')(number_advice.state_prices)
costs.command('predict')(delivery.routing_predict)
costs.add_typer(charge_commands.charges, name='charges')
costs.add_typer(charge_commands.invoices, name='invoices')

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
# Suggested packs from what Faxbot knows: the Setup page's Suggested packs.
system.add_typer(setup_plan.setup, name='setup')
system.add_typer(settings.settings, name='settings')
checks = _copy(settings.diagnostics)
checks.command('test-fax')(fax.inbound_simulate)
system.add_typer(checks, name='diagnostics')
system.command('health')(settings.health)
system.add_typer(operations.logs, name='logs')
system.command('audit')(access.audit_list)
system.command('restart')(operations.restart)
system.add_typer(codec.tools, name='codec')
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
        if name == 'sent':
            # The Faxes area's third page.
            app.add_typer(forms.forms, name='forms')
            # Faxes -> Expected: faxes recorded before they arrive, and recovery after a source system's outage.
            app.add_typer(expected_commands.expected, name='expected')
