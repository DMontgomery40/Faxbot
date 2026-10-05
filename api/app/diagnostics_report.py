"""Diagnostics an administrator can act on: live, read-only checks, each one sentence and one fix.

``POST /admin/diagnostics/report`` runs every check now; ``GET`` returns the last
run without contacting anything. Checks only read: provider sign-ins use each
provider's read-only account call, email uses an SMTP sign-in with no message,
the trunk uses Asterisk's status. Nothing sends a fax or costs money.

A check is an async function registered with :func:`check`; it receives a
:class:`Context` and returns :class:`Finding` values. Other features add their
own checks with the same decorator.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import smtplib
import ssl
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request

from .access.route_policy import require_permission
from .people_time import installation_zone_name, short as short_time
from .provider_labels import provider_label

router = APIRouter(prefix='/admin/diagnostics', tags=['Diagnostics'])

OK, ATTENTION, PROBLEM, OFF = 'ok', 'attention', 'problem', 'off'
SECTIONS = (('sending', 'Sending'), ('receiving', 'Receiving'), ('engine', 'Fax engine'),
            ('server', 'This server'), ('security', 'Security'))
CHECK_SECONDS = 10.0  # one slow provider never holds up the page
LOW_DISK_BYTES = 2 * 1024 ** 3


@dataclass(frozen=True)
class Finding:
    id: str
    section: str
    title: str
    status: str
    sentence: str
    fix_label: str | None = None
    fix_page: str | None = None  # a console address such as 'providers/sending'

    def as_dict(self):
        item = asdict(self)
        item['fix'] = ({'label': self.fix_label, 'page': self.fix_page} if self.fix_label else None)
        del item['fix_label'], item['fix_page']
        return item


@dataclass
class Context:
    request: Request
    identity: Any
    now: datetime = field(default_factory=datetime.utcnow)


Check = Callable[[Context], Awaitable[list[Finding]]]
_CHECKS: list[tuple[str, Check]] = []
_last: dict[str, Any] | None = None
_last_lock = threading.Lock()


def check(name: str):
    """Register a diagnostics check under a stable name (used when it fails to run)."""
    def register(function: Check) -> Check:
        _CHECKS.append((name, function))
        return function
    return register


def _main():
    from . import main  # the application module; imported late to avoid a cycle
    return main


def _when(moment):
    return short_time(moment) if moment else ''


def _day(moment):
    """'3 October' in the installation's time zone, from a datetime or an ISO text; '' when unknown."""
    from .people_time import zone
    if isinstance(moment, str):
        try:
            moment = datetime.fromisoformat(moment.replace('Z', '+00:00'))
        except ValueError:
            return ''
    if not isinstance(moment, datetime):
        return ''
    if moment.tzinfo is None:
        from datetime import timezone
        moment = moment.replace(tzinfo=timezone.utc)
    local = moment.astimezone(zone(installation_zone_name()))
    return f'{local.day} {local:%B}'


async def _blocking(function, *args):
    return await asyncio.to_thread(function, *args)


# ---------------------------------------------------------------------------
# Sending and receiving accounts
# ---------------------------------------------------------------------------

def _profile(request, direction):
    """The captured provider configuration for a direction, or None when none is set."""
    main = _main()
    snapshot = request.scope['faxbot.configuration']
    profile_id = snapshot.active.profile_id(direction)
    if profile_id is None:
        return None
    try:
        return main._configuration_manager().store.read_profile(profile_id).configuration
    except Exception:
        return False


async def _sign_in(configuration):
    """(status, sentence) from the provider's read-only account call."""
    from .config_profiles import ProviderProfile
    from .provider_execution import ProviderExecutionError, service_from_profile
    name = provider_label(configuration.provider_id)
    try:
        service = service_from_profile(ProviderProfile('diagnostics', 'diagnostics', configuration))
    except ProviderExecutionError:
        return PROBLEM, f'Faxbot cannot use the saved {name} settings. Open Providers and save them again.'
    if not service.is_configured():
        return PROBLEM, f'Some of {name}\'s sign-in details are missing. Add them in Providers.'
    provider = configuration.provider_id
    try:
        if provider == 'humblefax':
            numbers = await asyncio.wait_for(service.account_numbers(), CHECK_SECONDS)
            shown = ', '.join(numbers[:2])
            return OK, (f'{name} accepted Faxbot\'s sign-in' + (f' for {shown}.' if shown else '.'))
        if provider == 'efax':
            await asyncio.wait_for(service.authenticate(), CHECK_SECONDS)
            return OK, f'{name} accepted Faxbot\'s sign-in.'
    except asyncio.TimeoutError:
        return ATTENTION, f'{name} did not answer within {int(CHECK_SECONDS)} seconds. Check again in a minute.'
    except Exception as error:  # the services raise their own credential errors
        if 'credential' in type(error).__name__.lower() or 'auth' in type(error).__name__.lower():
            return PROBLEM, f'{name} did not accept Faxbot\'s sign-in details. Enter them again in Providers.'
        return ATTENTION, f'Faxbot could not reach {name} just now. Check this server\'s internet connection.'
    return OK, f'{name} has its sign-in details. The next fax shows whether {name} accepts them.'


@check('sending')
async def sending(context: Context) -> list[Finding]:
    configuration = _profile(context.request, 'outbound')
    if configuration is None:
        return [Finding('sending.provider', 'sending', 'Sending', PROBLEM,
                        'No sending provider is set up, so Faxbot cannot send faxes.',
                        'Set up sending', 'system/setup')]
    if configuration is False:
        return [Finding('sending.provider', 'sending', 'Sending', PROBLEM,
                        'Faxbot cannot read its saved sending settings. Open Providers and save them again.',
                        'Open Providers', 'providers/sending')]
    if configuration.provider_id in {'sip', 'freeswitch'}:
        name = provider_label(configuration.provider_id)
        return [Finding('sending.provider', 'sending', 'Sending', OK,
                        f'Faxes are sent through {name}. Fax engine, below, shows whether {name} accepts Faxbot\'s calls.')]
    status, sentence = await _sign_in(configuration)
    return [Finding('sending.provider', 'sending', 'Sending account', status, sentence,
                    None if status == OK else 'Open Providers', None if status == OK else 'providers/sending')]


@check('receiving')
async def receiving(context: Context) -> list[Finding]:
    main = _main()
    if not main.settings.inbound_enabled:
        return [Finding('receiving.provider', 'receiving', 'Receiving', OFF, 'Receiving is turned off.',
                        'Open Providers', 'providers/sending')]
    configuration = _profile(context.request, 'inbound')
    if not configuration:
        return [Finding('receiving.provider', 'receiving', 'Receiving', PROBLEM,
                        'Receiving is turned on, but no fax service is chosen to receive faxes. Choose one in Setup.',
                        'Set up receiving',
                        'system/setup')]
    if configuration.provider_id in {'sip', 'freeswitch'}:
        return []  # the trunk check below covers it
    outbound = _profile(context.request, 'outbound')
    if outbound and outbound.provider_id == configuration.provider_id and outbound.credentials == configuration.credentials:
        name = provider_label(configuration.provider_id)
        return [Finding('receiving.provider', 'receiving', 'Receiving account', OK,
                        f'{name} receives faxes with the same account it sends with.')]
    status, sentence = await _sign_in(configuration)
    return [Finding('receiving.provider', 'receiving', 'Receiving account', status, sentence,
                    None if status == OK else 'Open Providers', None if status == OK else 'providers/sending')]


# ---------------------------------------------------------------------------
# Recent faxes
# ---------------------------------------------------------------------------

def _store_engine():
    return _main()._configuration_manager().store.engine


def _sent_summary(now):
    # Typed columns: SQLite returns untyped dates as text.
    deliveries = sa.table('outbound_deliveries', sa.column('state', sa.String), sa.column('updated_at', sa.DateTime))
    week = now - timedelta(days=7)
    with _store_engine().connect() as connection:
        counts = dict(connection.execute(
            sa.select(deliveries.c.state, sa.func.count()).where(deliveries.c.updated_at >= week)
            .group_by(deliveries.c.state)).all())
        waiting = connection.execute(sa.select(sa.func.count()).select_from(deliveries)
                                     .where(deliveries.c.state == 'reconciliation_required')).scalar_one()
        last_ok = connection.execute(sa.select(sa.func.max(deliveries.c.updated_at))
                                     .where(deliveries.c.state == 'success')).scalar_one()
        last_failed = connection.execute(sa.select(sa.func.max(deliveries.c.updated_at))
                                         .where(deliveries.c.state == 'failed')).scalar_one()
    return counts, int(waiting or 0), last_ok, last_failed


def _plural(count, word):
    return f'{count} {word}' + ('' if count == 1 else 's')


@check('recent sent')
async def recent_sent(context: Context) -> list[Finding]:
    counts, waiting, last_ok, last_failed = await _blocking(_sent_summary, context.now)
    findings = []
    if waiting:
        findings.append(Finding('sending.confirm', 'sending', 'Faxes to confirm', ATTENTION,
                                f'{_plural(waiting, "fax")} may or may not have arrived. Faxbot will not send '
                                'them again on its own; confirm each one in Sent.', 'Open Sent', 'faxes/sent'))
    delivered, failed = counts.get('success', 0), counts.get('failed', 0)
    if not delivered and not failed and not last_ok:
        findings.append(Finding('sending.recent', 'sending', 'Recent faxes', OFF, 'No fax has been sent yet.'))
        return findings
    week = f'In the last 7 days: {delivered} delivered, {failed} failed.'
    if last_failed and (not last_ok or last_failed > last_ok):
        findings.append(Finding('sending.recent', 'sending', 'Recent faxes', ATTENTION,
                                f'The last fax failed ({_when(last_failed)}). {week}', 'Open Sent', 'faxes/sent'))
    else:
        findings.append(Finding('sending.recent', 'sending', 'Recent faxes', OK,
                                f'Last fax delivered {_when(last_ok)}. {week}'))
    return findings


def _received_summary(now):
    imports = sa.table('inbound_imports', sa.column('source', sa.String), sa.column('state', sa.String),
                       sa.column('acquired_at', sa.DateTime), sa.column('updated_at', sa.DateTime))
    real = imports.c.source != 'test'
    with _store_engine().connect() as connection:
        last = connection.execute(sa.select(sa.func.max(imports.c.acquired_at)).where(real)).scalar_one()
        week = connection.execute(sa.select(sa.func.count()).select_from(imports).where(
            real, imports.c.acquired_at >= now - timedelta(days=7))).scalar_one()
        failed = connection.execute(sa.select(sa.func.count()).select_from(imports).where(
            real, imports.c.state == 'failed')).scalar_one()
    return last, int(week or 0), int(failed or 0)


@check('recent received')
async def recent_received(context: Context) -> list[Finding]:
    if not _main().settings.inbound_enabled:
        return []
    last, week, failed = await _blocking(_received_summary, context.now)
    findings = []
    if failed:
        findings.append(Finding('receiving.failed', 'receiving', 'Faxes not fetched', PROBLEM,
                                f'Faxbot stopped trying to fetch {_plural(failed, "received fax")}. Open each one in '
                                'Received and select Fetch again.', 'Open Received', 'faxes/received'))
    if last:
        findings.append(Finding('receiving.recent', 'receiving', 'Recent faxes', OK,
                                f'Last fax received {_when(last)}. In the last 7 days: {week} received.'))
    else:
        findings.append(Finding('receiving.recent', 'receiving', 'Recent faxes', OFF, 'No fax has been received yet.'))
    return findings


# ---------------------------------------------------------------------------
# Email delivery of received faxes
# ---------------------------------------------------------------------------

def smtp_sign_in(settings, password, *, timeout=CHECK_SECONDS, context=None):
    """Connect, secure and sign in to the email server, then leave; never sends a message.

    Returns None when the server accepted, else one sentence saying what failed.
    """
    context = context or ssl.create_default_context()
    try:
        if settings.security == 'tls':
            client = smtplib.SMTP_SSL(settings.host, settings.port, timeout=timeout, context=context)
        else:
            client = smtplib.SMTP(settings.host, settings.port, timeout=timeout)
    except (OSError, smtplib.SMTPException):
        return 'Faxbot could not reach the email server.'
    try:
        client.ehlo()
        if settings.security == 'starttls':
            client.starttls(context=context)
            client.ehlo()
        if settings.username:
            client.login(settings.username, password)
        return None
    except smtplib.SMTPAuthenticationError:
        return 'The email server did not accept the user name or password.'
    except smtplib.SMTPNotSupportedError:
        return 'The email server does not support the chosen security setting.'
    except (OSError, smtplib.SMTPException):
        return 'The email server hung up while Faxbot was signing in.'
    finally:
        try:
            client.quit()
        except (OSError, smtplib.SMTPException):
            client.close()


def _intake_state(request):
    from .intake.http import _store
    store = _store(request)
    connectors = [item for item in store.list_connectors() if item.enabled]
    return store, connectors, store.counts()


@check('email delivery')
async def email_delivery(context: Context) -> list[Finding]:
    if not _main().settings.inbound_enabled:
        return []
    store, connectors, counts = await _blocking(_intake_state, context.request)
    if not connectors:
        return [Finding('receiving.email', 'receiving', 'Email delivery', OFF,
                        'Received faxes are not emailed to anyone.', 'Set up email delivery', 'numbers/email')]
    findings = []
    for connector in connectors:
        password = await _blocking(store.password, connector.id)
        try:
            problem = await asyncio.wait_for(_blocking(smtp_sign_in, connector.settings, password),
                                             CHECK_SECONDS + 2)
        except asyncio.TimeoutError:
            problem = 'The email server did not answer in time.'
        recipients = ', '.join(connector.settings.recipients)
        findings.append(Finding(f'receiving.email.{connector.id}', 'receiving', f'Email to {recipients}',
                                PROBLEM if problem else OK,
                                problem or 'Faxbot can sign in to the email server.',
                                'Open Email delivery' if problem else None, 'numbers/email' if problem else None))
    failed = int(counts.get('failed', 0))
    if failed:
        findings.append(Finding('receiving.email.failed', 'receiving', 'Faxes not emailed', ATTENTION,
                                f'{_plural(failed, "received fax")} could not be emailed. Retry them from Received.',
                                'Open Received', 'faxes/received'))
    return findings


# ---------------------------------------------------------------------------
# Fax engine and carrier trunk
# ---------------------------------------------------------------------------

def _uses_trunk(request):
    for direction in ('outbound', 'inbound'):
        configuration = _profile(request, direction)
        if configuration and configuration.provider_id == 'sip':
            return True
    return False


@check('fax engine')
async def fax_engine(context: Context) -> list[Finding]:
    main = _main()
    from .ami import ami_client
    required = main.providerHasTrait('any', 'requires_ami')
    if not required and not _uses_trunk(context.request):
        return [Finding('engine.running', 'engine', 'Fax engine', OFF,
                        'Your providers send and receive faxes themselves, so Faxbot\'s own fax engine is not used.')]
    if not ami_client._connected.is_set():
        message = ami_client.engine_message() or 'Faxbot cannot reach its fax engine.'
        return [Finding('engine.running', 'engine', 'Fax engine', PROBLEM, message, 'Open carrier trunk',
                        'providers/trunk')]
    try:
        calls = await asyncio.wait_for(ami_client.active_calls(), CHECK_SECONDS)
        detail = 'No call is in progress.' if not calls else f'{_plural(calls, "call")} in progress.'
    except Exception:
        detail = ''
    return [Finding('engine.running', 'engine', 'Fax engine', OK, f'The fax engine is running. {detail}'.strip())]


@check('ssl fax engine')
async def ssl_fax_engine(context: Context) -> list[Finding]:
    """The SSL Fax engine (HylaFAX+): running, its lines on Asterisk, and whether its listener is published."""
    if not _uses_trunk(context.request):
        return []
    from . import hylafax_engine
    from .ami import ami_client
    from .config import configuration_values
    state, sentence = await hylafax_engine.engine_summary(configuration_values(), ami_client)
    status = {'running': OK, 'starting': ATTENTION, 'not_set_up': OFF}.get(state, PROBLEM)
    fix = None if status in (OK, OFF) else 'Open carrier trunk'
    return [Finding('engine.sslfax', 'engine', 'Fast fax service', status, sentence, fix,
                    'providers/trunk' if fix else None)]


@check('carrier trunk')
async def carrier_trunk(context: Context) -> list[Finding]:
    if not _uses_trunk(context.request):
        return []
    from . import sip_http
    status = await asyncio.wait_for(sip_http.status(context.request, context.identity), CHECK_SECONDS * 2)
    carrier = status.get('preset_label') or 'Your carrier'
    registration = status.get('registration')
    working = status.get('asterisk_connected') and registration in {'registered', 'not_used'} and (
        registration == 'registered' or status.get('reachability') == 'reachable')
    findings = [Finding('engine.trunk', 'engine', f'{carrier} trunk', OK if working else PROBLEM,
                        status.get('message') or status.get('registration_text') or '',
                        None if working else 'Open carrier trunk', None if working else 'providers/trunk')]
    if status.get('handover_text'):
        ready = status.get('handover_ready')
        findings.append(Finding('engine.handover', 'receiving', 'Faxes from the trunk', OK if ready else PROBLEM,
                                status['handover_text'], None if ready else 'Open carrier trunk',
                                None if ready else 'providers/trunk'))
    if status.get('t38_off_reason'):
        from . import sip_fax_mode
        sentence = sip_fax_mode.off_sentence(status['t38_off_reason'], _day(status.get('t38_off_at')), carrier)
        findings.append(Finding('engine.t38', 'engine', 'Fax over IP (T.38)', ATTENTION,
                                sentence or 'Faxbot uses audio fax on this trunk.', 'Open carrier trunk',
                                'providers/trunk'))
    if status.get('last_call_text'):
        verdict = status.get('last_call_verdict') or ''
        good = verdict in {'sent', 'received', ''}
        findings.append(Finding('engine.last_call', 'engine', 'Last call', OK if good else ATTENTION,
                                status['last_call_text']))
    return findings


@check('network for fax over IP')
async def network_for_fax(context: Context) -> list[Finding]:
    """The last network check for fax over IP (sip_network): whether T.38 fax data can come back."""
    if not _uses_trunk(context.request):
        return []
    from . import sip_network
    from .config import configuration_values
    found = await _blocking(sip_network.report, configuration_values())
    if not found.get('applies'):
        return []
    good = found['t38'] == sip_network.OPEN or not found.get('checked')
    return [Finding('engine.network', 'engine', 'Faxing over the internet', OK if good else ATTENTION,
                    found.get('office_text') or '', None if good else 'Open carrier trunk',
                    None if good else 'providers/trunk')]


# ---------------------------------------------------------------------------
# This server
# ---------------------------------------------------------------------------

def _gigabytes(count):
    return f'{count / 1024 ** 3:.1f} GB'


def _server_facts():
    main = _main()
    settings = main.settings
    facts = {'gs': shutil.which('gs') is not None}
    try:
        usage = shutil.disk_usage(settings.fax_data_dir)
        facts['disk'] = (usage.free, usage.total)
    except OSError:
        facts['disk'] = None
    for name, folder in (('data_writable', settings.fax_data_dir), ('temp_writable', None)):
        try:
            with tempfile.NamedTemporaryFile(dir=folder, prefix='faxbot-diagnostic-', delete=True) as probe:
                probe.write(b'ok')
                probe.flush()
            facts[name] = True
        except OSError:
            facts[name] = False
    readiness = None
    try:
        from sqlalchemy import text
        with main.SessionLocal() as db:
            db.execute(text('SELECT 1'))
        readiness = True
    except Exception:
        readiness = False
    facts['database'] = readiness
    facts['kind'] = 'PostgreSQL' if settings.database_url.startswith('postgres') else 'a database file on this server'
    return facts


@check('server')
async def server(context: Context) -> list[Finding]:
    facts = await _blocking(_server_facts)
    findings = [Finding('server.database', 'server', 'Database', OK if facts['database'] else PROBLEM,
                        'Faxbot can read and save its records.' if facts['database']
                        else 'Faxbot cannot read or save its records, so it cannot show or keep faxes.')]
    if facts['disk']:
        free, total = facts['disk']
        low = free < LOW_DISK_BYTES or free < total * 0.05
        findings.append(Finding('server.disk', 'server', 'Disk space', ATTENTION if low else OK,
                                f'{_gigabytes(free)} free of {_gigabytes(total)} where faxes are stored.'
                                + (' Free some space soon: Faxbot stops receiving when the disk is full.' if low else ''),
                                'Open Storage & retention' if low else None, 'system/storage' if low else None))
    folders = facts['data_writable'] and facts['temp_writable']
    findings.append(Finding('server.folders', 'server', 'Fax folders', OK if folders else PROBLEM,
                            'Faxbot can save fax files.' if folders
                            else 'Faxbot cannot save fax files on this server. Ask whoever runs the server to '
                                 'check its disk.'))
    findings.append(Finding('server.converter', 'server', 'Document converter', OK if facts['gs'] else PROBLEM,
                            'Faxbot can turn documents into fax pages.' if facts['gs']
                            else 'Faxbot\'s document converter is missing, so it cannot turn documents into fax pages.'))
    zone = installation_zone_name()
    findings.append(Finding('server.time_zone', 'server', 'Time zone', OK if zone else ATTENTION,
                            f'Times are shown in {zone} time.' if zone
                            else 'No time zone is chosen, so times are shown in world standard time (UTC).',
                            None if zone else 'Choose a time zone', None if zone else 'system/setup'))
    snapshot = context.request.scope['faxbot.configuration']
    if snapshot.pending is not None:
        findings.append(Finding('server.restart', 'server', 'Waiting for a restart', ATTENTION,
                                'Some saved changes take effect after Faxbot restarts.'))
    main = _main()
    settings = main.settings
    if settings.storage_backend.lower() == 's3':
        ready = bool(settings.s3_bucket)
        findings.append(Finding('server.storage', 'server', 'Online storage', OK if ready else PROBLEM,
                                'Received faxes are kept in online storage.' if ready
                                else 'Online storage is turned on but not finished. Finish it in Storage & retention.',
                                None if ready else 'Open Storage & retention', None if ready else 'system/storage'))
    return findings


@check('security')
async def security(context: Context) -> list[Finding]:
    settings = _main().settings
    findings = [
        Finding('security.audit', 'security', 'Audit log', OK if settings.audit_log_enabled else ATTENTION,
                'Faxbot records who changed what in the audit log.' if settings.audit_log_enabled
                else 'Faxbot is not recording who changed what. Turn on the audit log.',
                None if settings.audit_log_enabled else 'Open Audit log',
                None if settings.audit_log_enabled else 'system/audit'),
        Finding('security.limits', 'security', 'Protection from overload',
                OK if settings.max_requests_per_minute > 0 else ATTENTION,
                'Faxbot protects itself from a program that sends it too much at once.'
                if settings.max_requests_per_minute > 0
                else ('Faxbot does not protect itself from a program that sends it too much at once. Turn this on '
                      'in Security.'),
                None if settings.max_requests_per_minute > 0 else 'Open Security',
                None if settings.max_requests_per_minute > 0 else 'system/security'),
    ]
    if not settings.enforce_public_https:
        findings.append(Finding('security.https', 'security', 'Secure links', ATTENTION,
                                'Fax services may collect your documents over an unprotected connection. Turn on secure links in '
                                'Security.',
                                'Open Security', 'system/security'))
    return findings


# ---------------------------------------------------------------------------
# Running and reporting
# ---------------------------------------------------------------------------

def _verdict(findings):
    problems = sum(item.status == PROBLEM for item in findings)
    attention = sum(item.status == ATTENTION for item in findings)
    if problems:
        return PROBLEM, (f'{_plural(problems, "problem")} {"keeps" if problems == 1 else "keep"} Faxbot from working fully'
                         + (f', and {attention} more {"thing needs" if attention == 1 else "things need"} attention.'
                            if attention else '.'))
    if attention:
        return ATTENTION, (f'Faxbot is working. {attention} '
                           f'{"thing needs" if attention == 1 else "things need"} attention.')
    return OK, 'Everything Faxbot checked is working.'


async def _run_one(name, function, context):
    try:
        return await asyncio.wait_for(function(context), CHECK_SECONDS * 3)
    except Exception:
        return [Finding(f'check.{name.replace(" ", "_")}', 'server', name.capitalize(), ATTENTION,
                        f'Faxbot could not finish the {name} check. Check again in a minute.')]


async def run(request: Request, identity) -> dict[str, Any]:
    """Run every registered check now and keep the result for later reads."""
    global _last
    context = Context(request, identity)
    results = await asyncio.gather(*(_run_one(name, function, context) for name, function in _CHECKS))
    findings = [item for group in results for item in group]
    status, sentence = _verdict(findings)
    report = {
        'checked_at': context.now.isoformat() + 'Z',
        'checked_at_text': _when(context.now),
        'status': status,
        'summary': sentence,
        'sections': [{'id': key, 'title': title,
                      'checks': [item.as_dict() for item in findings if item.section == key]}
                     for key, title in SECTIONS],
    }
    report['sections'] = [section for section in report['sections'] if section['checks']]
    with _last_lock:
        _last = report
    return report


# Read-only views of the fax engine for System → Developer → Scripts & checks.
ENGINE_VIEWS = {
    'registrations': ('Trunk sign-ins', {'Action': 'PJSIPShowRegistrationsOutbound'}, 'OutboundRegistrationDetail',
                      (('ObjectName', 'Name'), ('Status', 'Status'), ('ServerUri', 'Server'), ('Transport', 'Transport'))),
    # The trunk's fixed contact appears only under its endpoint, not in PJSIPShowContacts.
    'contacts': ('Addresses the fax engine checks', {'Action': 'PJSIPShowEndpoint', 'Endpoint': 'trunk-endpoint'},
                 'ContactStatusDetail', (('URI', 'Address'), ('Status', 'Status'), ('RoundtripUsec', 'Round trip (ms)'))),
    'calls': ('Calls in progress', {'Action': 'CoreShowChannels'}, 'CoreShowChannel',
              (('Channel', 'Call'), ('ChannelStateDesc', 'State'), ('CallerIDNum', 'Caller'), ('Exten', 'Number'),
               ('Duration', 'Duration'))),
    'faxes': ('Faxes in progress', {'Action': 'FAXSessions'}, 'FAXSessionsEntry',
              (('Channel', 'Call'), ('Technology', 'Way'), ('SessionType', 'Kind'), ('Operation', 'Direction'),
               ('State', 'State'))),
}


def _cell(key, value):
    if key == 'RoundtripUsec' and str(value).strip().isdigit():
        return str(max(1, round(int(value) / 1000)))
    return str(value)


async def engine_rows(view):
    """One engine view as {available, title, columns, rows, message}; never a secret."""
    from .ami import ami_client
    title, fields, event, columns = ENGINE_VIEWS[view]
    result = {'view': view, 'title': title, 'columns': [label for _, label in columns], 'rows': [],
              'available': False, 'message': None}
    if not ami_client._connected.is_set():
        result['message'] = ami_client.engine_message() or 'Faxbot is not connected to its fax engine.'
        return result
    try:
        response, events = await ami_client.status_query(dict(fields), collect=True)
    except (ConnectionError, TimeoutError):
        result['message'] = 'The fax engine did not answer. Check again in a moment.'
        return result
    if response['response'].lower() != 'success' and view == 'contacts' and 'unable to retrieve' in str(
            response.get('message', '')).lower():
        result['available'], result['message'] = True, 'No carrier trunk is set up in the fax engine.'
        return result
    if response['response'].lower() != 'success':
        message = response.get('message', '')
        result['message'] = ('Faxbot\'s fax engine login may not read this.' if 'permission' in message.lower()
                             else 'The fax engine could not list this.')
        return result
    keys = [key for key, _ in columns]
    result['rows'] = [[_cell(key, item.get(key, '')) for key in keys] for item in events
                      if any(key in item for key in keys) and (event != 'CoreShowChannel' or item.get('Channel'))]
    result['available'] = True
    if not result['rows']:
        result['message'] = {'registrations': 'The fax engine signs in to no trunk.',
                             'contacts': 'The fax engine checks no addresses.',
                             'calls': 'No call is in progress.', 'faxes': 'No fax is in progress.'}[view]
    return result


@router.get('/engine/{view}')
async def engine_view(view: str, identity=Depends(require_permission('diagnostics:read'))):
    """A read-only list from the fax engine: registrations, contacts, calls or faxes."""
    if view not in ENGINE_VIEWS:
        from fastapi import HTTPException
        raise HTTPException(404, detail='Choose registrations, contacts, calls or faxes.')
    return await engine_rows(view)


@router.get('/report')
async def last_report(identity=Depends(require_permission('diagnostics:read'))):
    """The last diagnostics run, without contacting anything; empty until the first run."""
    with _last_lock:
        return _last or {'checked_at': None, 'checked_at_text': '', 'status': None, 'summary': None, 'sections': []}


@router.post('/report')
async def run_report(request: Request, identity=Depends(require_permission('diagnostics:read'))):
    """Run every check now. Read-only: no fax is sent and nothing is changed."""
    return await run(request, identity)
