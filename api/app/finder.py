"""Find a page or a setting by the words for the job (#53).

One catalogue serves the console's search box and `faxbot find`: every page in the
console's navigation table (admin_ui/src/navigation.tsx, mirrored below and held to it
by tests/test_finder.py) and every settings field, each with the one place it is
changed and its scope. Searching matches page and field labels, technical names and
synonyms only — never document content, never a setting's value — and a result is
offered only when this person may open its page, so forbidden pages and fields never
appear. The console and the command read the same answers from GET /auth/finder.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .config_values import ConfigurationValues

SETTINGS_READ = ('settings:read',)
OVERVIEW = ('diagnostics:read', 'settings:read')


@dataclass(frozen=True)
class Page:
    area: str
    page: str
    label: str
    area_label: str
    group: str | None = None
    any_of: tuple[str, ...] = ()
    navigation: tuple[str, ...] = ()
    synonyms: tuple[str, ...] = ()
    # An entry that opens another page, such as Add or change a provider (the Setup wizard).
    link: str | None = None

    @property
    def address(self) -> str:
        return self.link or (f'#/{self.area}' if self.area == self.page else f'#/{self.area}/{self.page}')

    @property
    def place(self) -> str:
        return ' › '.join(part for part in (self.area_label, self.group, self.label) if part)


def _page(area, page, label, area_label, **kwargs):
    return Page(area, page, label, area_label, **kwargs)


# The console's navigation table, area by area, in menu order: label, group, who may open
# it (the same gate the table states), and the other words people use for it.
PAGES = (
    _page('overview', 'overview', 'Overview', 'Overview', any_of=OVERVIEW,
          synonyms=('home', 'dashboard', 'what faxbot is doing', 'needs attention')),
    _page('savings', 'capabilities', 'Capabilities', 'Savings & optimization', any_of=SETTINGS_READ,
          synonyms=('mechanisms', 'what faxbot can do', 'features', 'turn on')),
    _page('savings', 'opportunities', 'Opportunities', 'Savings & optimization', any_of=SETTINGS_READ,
          synonyms=('recommendations', 'suggestions', 'advice', 'next improvements')),
    _page('savings', 'facts', 'Facts to establish', 'Savings & optimization', any_of=SETTINGS_READ,
          synonyms=('missing facts', 'advice', 'what is missing')),
    _page('savings', 'results', 'Savings results', 'Savings & optimization', any_of=SETTINGS_READ,
          synonyms=('savings', 'saved', 'results')),
    _page('savings', 'spending', 'Spending', 'Savings & optimization', any_of=SETTINGS_READ,
          synonyms=('costs', 'what faxing costs', 'routes')),
    _page('savings', 'charges', 'Charges', 'Savings & optimization', any_of=SETTINGS_READ,
          synonyms=('bills', 'carrier charges', 'charged')),
    _page('savings', 'invoices', 'Invoices', 'Savings & optimization', any_of=SETTINGS_READ,
          synonyms=('monthly bill', 'invoice totals')),
    _page('savings', 'prices', 'Prices & plans', 'Savings & optimization', any_of=SETTINGS_READ,
          synonyms=('rates', 'rate cards', 'plans', 'prices')),
    _page('faxes', 'received', 'Received', 'Faxes', navigation=('inbox', 'work'),
          synonyms=('inbox', 'incoming faxes', 'received faxes', 'owners')),
    _page('faxes', 'sent', 'Sent', 'Faxes', navigation=('jobs',),
          synonyms=('jobs', 'sent faxes', 'outgoing faxes', 'held faxes')),
    _page('faxes', 'send', 'Send a fax', 'Faxes', navigation=('send',),
          synonyms=('send', 'new fax', 'compose')),
    _page('faxes', 'expected', 'Expected', 'Faxes', any_of=('work:read', 'work:import'),
          synonyms=('expected faxes', 'awaiting', 'overdue faxes', 'import work')),
    _page('faxes', 'forms', 'Forms', 'Faxes', any_of=('settings:read', 'fax:send'),
          synonyms=('registered forms', 'fill in a form')),
    _page('faxes', 'cases', 'Case packets', 'Faxes', any_of=SETTINGS_READ,
          synonyms=('cases', 'packets', 'checklists', 'accepted documents')),
    _page('delivery', 'numbers', 'Numbers', 'Delivery setup', group='Numbers & mailboxes',
          any_of=('mailboxes:read', 'mailboxes:manage'), synonyms=('fax numbers', 'your numbers', 'dids')),
    _page('delivery', 'mailboxes', 'Mailboxes', 'Delivery setup', group='Numbers & mailboxes',
          any_of=('mailboxes:read', 'mailboxes:manage', 'settings:read'),
          synonyms=('who can send', 'sending rules', 'acknowledge by', 'mailbox owners')),
    _page('delivery', 'moves', 'Number moves', 'Delivery setup', group='Numbers & mailboxes',
          any_of=SETTINGS_READ, synonyms=('move a number', 'port a number', 'advice and moves')),
    _page('delivery', 'blocked', 'Blocked senders', 'Delivery setup', group='Numbers & mailboxes',
          any_of=SETTINGS_READ, synonyms=('block a sender', 'junk faxes', 'spam')),
    _page('delivery', 'identity', 'Sending identity', 'Delivery setup', group='Numbers & mailboxes',
          any_of=SETTINGS_READ, synonyms=('caller ID', 'sender identity', 'station ID', 'reply number', 'header')),
    _page('delivery', 'email', 'Staff email delivery', 'Delivery setup', group='Documents in and out',
          any_of=SETTINGS_READ, synonyms=('email delivery', 'SMTP', 'deliver faxes by email', 'staff email')),
    _page('delivery', 'connectors', 'Email and folders', 'Delivery setup', group='Documents in and out',
          any_of=SETTINGS_READ, synonyms=('connectors', 'intake', 'folders', 'email to fax', 'import documents')),
    _page('delivery', 'connections', 'Providers & accounts', 'Delivery setup', group='Connections',
          any_of=SETTINGS_READ, synonyms=('providers', 'accounts', 'in use', 'sending provider', 'receiving provider')),
    _page('delivery', 'rules', 'Routing rules', 'Delivery setup', group='Connections',
          any_of=SETTINGS_READ, synonyms=('rules', 'who can send', 'which account sends', 'routing')),
    _page('delivery', 'humblefax', 'HumbleFax', 'Delivery setup', group='Connections', any_of=SETTINGS_READ,
          synonyms=('HumbleFax account',)),
    _page('delivery', 'efax', 'eFax', 'Delivery setup', group='Connections', any_of=SETTINGS_READ,
          synonyms=('eFax account',)),
    _page('delivery', 'phaxio', 'Phaxio', 'Delivery setup', group='Connections', any_of=SETTINGS_READ,
          synonyms=('Phaxio account',)),
    _page('delivery', 'sinch', 'Sinch', 'Delivery setup', group='Connections', any_of=SETTINGS_READ,
          synonyms=('Sinch account',)),
    _page('delivery', 'signalwire', 'SignalWire', 'Delivery setup', group='Connections', any_of=SETTINGS_READ,
          synonyms=('SignalWire account',)),
    _page('delivery', 'documo', 'Documo', 'Delivery setup', group='Connections', any_of=SETTINGS_READ,
          synonyms=('Documo account', 'mFax')),
    _page('delivery', 'telnyx-fax', 'Telnyx Fax API', 'Delivery setup', group='Connections', any_of=SETTINGS_READ,
          synonyms=('Telnyx', 'Telnyx Fax account')),
    _page('delivery', 'trunk', 'Carrier trunk', 'Delivery setup', group='Connections', any_of=SETTINGS_READ,
          synonyms=('T.38', 'SIP trunk', 'trunk', 'phone system', 'carrier', 'caller ID', 'fax line')),
    _page('delivery', 'change', 'Add or change a provider', 'Delivery setup', group='Connections',
          any_of=('settings:write',), link='#/admin/setup',
          synonyms=('add a provider', 'change provider', 'choose a provider', 'setup')),
    _page('recipients', 'list', 'Recipients', 'Recipients', any_of=SETTINGS_READ,
          synonyms=('numbers you send to', 'destinations', 'recipient settings')),
    _page('recipients', 'partners', 'Partners', 'Recipients', any_of=SETTINGS_READ,
          synonyms=('direct delivery', 'partners', 'verified partners', 'send over the internet')),
    _page('admin', 'users', 'Users', 'Administration', group='People & access',
          any_of=('users:read', 'users:manage'), synonyms=('people', 'accounts', 'passwords', 'add a user')),
    _page('admin', 'groups', 'Groups', 'Administration', group='People & access',
          any_of=('groups:read', 'groups:manage'), synonyms=('teams', 'groups of people')),
    _page('admin', 'roles', 'Roles', 'Administration', group='People & access',
          any_of=('roles:read', 'roles:manage'), synonyms=('permissions', 'what people may do')),
    _page('admin', 'who', 'Who has access', 'Administration', group='People & access',
          any_of=('grants:read', 'grants:manage'),
          synonyms=('who can send', 'who can', 'access', 'grants', 'who may')),
    _page('admin', 'keys', 'Keys & phones', 'Administration', group='People & access',
          any_of=('keys:manage',), synonyms=('API keys', 'keys', 'paired phones', 'installation key')),
    _page('admin', 'sessions', 'Sessions', 'Administration', group='People & access',
          synonyms=('sign-ins', 'my sessions', 'devices', 'logins')),
    _page('admin', 'setup', 'Setup', 'Administration', group='Installation',
          any_of=('settings:write',), synonyms=('setup wizard', 'first setup', 'choose a provider', 'time zone')),
    _page('admin', 'security', 'Security', 'Administration', group='Installation', any_of=SETTINGS_READ,
          synonyms=('HTTPS', 'security settings')),
    _page('admin', 'retention', 'Documents & retention', 'Administration', group='Installation',
          any_of=SETTINGS_READ,
          synonyms=('retention', 'storage', 'how long faxes are kept', 'delete old faxes', 'S3', 'backup')),
    _page('admin', 'analysis', 'AI analysis', 'Administration', group='Installation', any_of=SETTINGS_READ,
          synonyms=('AI', 'analysis', 'model', 'automatic review')),
    _page('admin', 'npi', 'NPI record', 'Administration', group='Installation', any_of=SETTINGS_READ,
          synonyms=('NPI', 'registry')),
    _page('admin', 'health', 'System health', 'Administration', group='Monitoring',
          any_of=('diagnostics:read',), synonyms=('diagnostics', 'health', 'checks', 'status')),
    _page('admin', 'audit', 'Audit log', 'Administration', group='Monitoring',
          any_of=('audit:read',), synonyms=('audit', 'who did what', 'history of changes')),
    _page('admin', 'logs', 'Logs', 'Administration', group='Monitoring',
          any_of=('logs:read',), synonyms=('server logs', 'errors')),
    _page('admin', 'api', 'API & SDKs', 'Administration', group='Developer', any_of=OVERVIEW,
          synonyms=('API', 'SDKs', 'developers', 'documentation address')),
    _page('admin', 'assistants', 'AI assistants', 'Administration', group='Developer', any_of=SETTINGS_READ,
          synonyms=('MCP', 'assistants', 'AI assistants')),
    _page('admin', 'terminal', 'Terminal', 'Administration', group='Developer',
          any_of=('host:terminal',), synonyms=('shell', 'command line on the server')),
    _page('admin', 'scripts', 'Scripts & checks', 'Administration', group='Developer',
          any_of=('providers:write',), synonyms=('scripts', 'tests', 'checks')),
    _page('admin', 'plugins', 'Provider plugins', 'Administration', group='Developer',
          any_of=('providers:read', 'settings:read'), synonyms=('plugins', 'extensions')),
)

BY_ADDRESS = {page.address: page for page in PAGES}


@dataclass(frozen=True)
class Setting:
    name: str
    label: str
    address: str | None
    place: str
    scope: str
    command: str
    synonyms: tuple[str, ...] = ()


# Settings with no console home, and the one place each is changed instead. This is the
# same list test_settings_coverage.py keeps (READ_ONLY_OR_ENV), plus the two FreeSWITCH
# fields the Setup wizard still names; the finder says where they are changed rather
# than hiding them.
NO_CONSOLE_HOME = {
    'api_key': 'the API_KEY variable in .env (owner recovery shows it as set there)',
    'database_url': 'the DATABASE_URL variable, through the maintenance transfer',
    'fax_data_dir': 'the FAX_DATA_DIR variable; the data folder is fixed for the installation',
    'faxbot_config_path': 'the older settings file, read once at first start',
    'require_api_key': 'nowhere: authentication is always required',
    'fs_esl_host': 'faxbot delivery providers configure freeswitch (FreeSWITCH has no console page)',
    'fs_esl_port': 'faxbot delivery providers configure freeswitch (FreeSWITCH has no console page)',
    'fs_esl_password': 'faxbot delivery providers configure freeswitch (FreeSWITCH has no console page)',
    'fs_t38_enable': 'faxbot delivery providers configure freeswitch (FreeSWITCH has no console page)',
    'fs_caller_id_number': 'faxbot delivery providers configure freeswitch (FreeSWITCH has no console page)',
    'fs_gateway_name': 'faxbot delivery providers configure freeswitch (FreeSWITCH has no console page)',
}

# A field's home by the start of its name: each provider's fields on that provider's
# page, the trunk's on the trunk page, and so on. FIELD_HOMES below wins where a name
# starts like a neighbour's.
PREFIX_HOMES = (
    ('phaxio_', '#/delivery/phaxio'), ('sinch_', '#/delivery/sinch'),
    ('documo_', '#/delivery/documo'), ('efax_', '#/delivery/efax'),
    ('humblefax_', '#/delivery/humblefax'), ('signalwire_', '#/delivery/signalwire'),
    ('telnyx_', '#/delivery/telnyx-fax'), ('sip_', '#/delivery/trunk'),
    ('ami_', '#/admin/setup'), ('intake_', '#/delivery/email'),
    ('direct_', '#/recipients/partners'), ('analysis_', '#/admin/analysis'),
    ('audit_log_', '#/admin/audit'), ('s3_', '#/admin/retention'),
)

FIELD_HOMES = {
    'admin_allow_restart': '#/admin/health',
    'artifact_ttl_days': '#/admin/retention',
    'asterisk_inbound_secret': '#/delivery/trunk',
    'case_suggestions_enabled': '#/faxes/cases',
    'cleanup_interval_minutes': '#/admin/retention',
    'docs_base_url': '#/admin/api',
    'enable_mcp_http': '#/admin/assistants',
    'enable_mcp_sse': '#/admin/assistants',
    'enable_persisted_settings': '#/admin/security',
    'enable_s3_diagnostics': '#/admin/health',
    'enforce_public_https': '#/admin/security',
    'fax_backend': '#/delivery/connections',
    'fax_default_country': '#/delivery/connections',
    'fax_disabled': '#/delivery/connections',
    'fax_friendly_documents': '#/delivery/connections',
    'fax_friendly_whiten': '#/delivery/connections',
    'fax_header': '#/delivery/identity',
    'fax_reply_number': '#/delivery/identity',
    'fax_reply_numbers': '#/delivery/identity',
    'fax_send_only_numbers': '#/delivery/trunk',
    'fax_station_id': '#/delivery/identity',
    'feature_plugin_install': '#/admin/plugins',
    'feature_v3_plugins': '#/admin/plugins',
    'flowroute_access_key': '#/delivery/connections',
    'flowroute_secret_key': '#/delivery/connections',
    'inbound_backend': '#/delivery/connections',
    'inbound_enabled': '#/delivery/connections',
    'inbound_get_rpm': '#/admin/retention',
    'inbound_list_rpm': '#/admin/retention',
    'inbound_retention_days': '#/admin/retention',
    'inbound_token_ttl_minutes': '#/admin/retention',
    'local_delivery_enabled': '#/delivery/email',
    'max_file_size_mb': '#/admin/retention',
    'max_requests_per_minute': '#/admin/retention',
    'mcp_http_path': '#/admin/assistants',
    'mcp_sse_path': '#/admin/assistants',
    'mobile_local_base': '#/admin/keys',
    'oauth_audience': '#/admin/assistants',
    'oauth_issuer': '#/admin/assistants',
    'oauth_jwks_url': '#/admin/assistants',
    'outbound_backend': '#/delivery/connections',
    'outbound_routes': '#/delivery/connections',
    'pdf_token_ttl_minutes': '#/admin/retention',
    'persisted_env_path': '#/admin/retention',
    'plan_budgets': '#/savings/prices',
    'providers_dir': '#/admin/plugins',
    'public_api_url': '#/admin/security',
    'received_registered_senders': '#/delivery/trunk',
    'require_mcp_oauth': '#/admin/assistants',
    'route_min_success_percent': '#/delivery/connections',
    'stir_trust_anchors': '#/delivery/rules',
    'storage_backend': '#/admin/retention',
    'time_zone': '#/admin/setup',
    'work_acknowledge_hours': '#/delivery/mailboxes',
}

# The words people use for a setting, beside its label and its technical name.
FIELD_SYNONYMS = {
    'sip_t38_enabled': ('T.38', 'fax over IP', 'FoIP'),
    'sip_t38_error_correction': ('T.38', 'error correction'),
    'sip_t38_max_datagram': ('T.38',),
    'fs_t38_enable': ('T.38',),
    'sip_trunk_caller_id': ('caller ID', 'calling number', 'from number'),
    'efax_caller_id': ('caller ID',),
    'fs_caller_id_number': ('caller ID',),
    'fax_station_id': ('station ID', 'CSID', 'sender name on the fax'),
    'fax_header': ('header line', 'fax header'),
    'inbound_retention_days': ('retention', 'how long received faxes are kept', 'delete received faxes'),
    'artifact_ttl_days': ('retention', 'how long documents are kept'),
    'cleanup_interval_minutes': ('retention', 'tidy up', 'cleanup'),
    'inbound_token_ttl_minutes': ('download link lifetime', 'retention'),
    'pdf_token_ttl_minutes': ('download link lifetime', 'retention'),
    'storage_backend': ('storage', 'where documents are kept', 'S3'),
    'work_acknowledge_hours': ('acknowledge by', 'acknowledgement target', 'owner deadline'),
    'time_zone': ('time zone', 'timezone', 'local time'),
    'fax_default_country': ('country', 'dialling country', 'number format'),
    'fax_disabled': ('turn sending off', 'disable sending'),
    'docs_base_url': ('documentation address', 'help links'),
    'mobile_local_base': ('phone address', 'paired phones'),
    'enforce_public_https': ('HTTPS', 'secure connection'),
    'admin_allow_restart': ('restart', 'restart from the console'),
}


def _label(name: str) -> str:
    text = name.replace('_', ' ')
    return text[:1].upper() + text[1:]


def _home(name: str) -> str | None:
    if name in FIELD_HOMES:
        return FIELD_HOMES[name]
    for prefix, address in PREFIX_HOMES:
        if name.startswith(prefix):
            return address
    return None


def setting_entries() -> tuple[Setting, ...]:
    """Every settings field with its one place, its scope and its command. Never a value."""
    entries = []
    for name, field in ConfigurationValues.model_fields.items():
        secret = bool((field.json_schema_extra or {}).get('secret'))
        command = (f'faxbot admin settings set --secret-stdin {name}' if secret
                   else f'faxbot admin settings set {name}=VALUE')
        if name in NO_CONSOLE_HOME:
            entries.append(Setting(name, _label(name), None, NO_CONSOLE_HOME[name],
                                   'Installation (set outside the console)', command,
                                   FIELD_SYNONYMS.get(name, ())))
            continue
        address = _home(name)
        assert address is not None, f'finder: no console home for {name}'
        page = BY_ADDRESS[address]
        entries.append(Setting(name, _label(name), address, page.place, 'Installation',
                               command, FIELD_SYNONYMS.get(name, ())))
    return tuple(entries)


def _words(text: str) -> tuple[str, ...]:
    """Search words for a phrase: 'T.38' matches as t38 as well as t + 38."""
    lowered = text.lower()
    parts = tuple(part for part in re.split(r'[^a-z0-9]+', lowered) if part)
    joined = ''.join(parts)
    return parts + ((joined,) if len(parts) > 1 and joined not in parts else ())


def _visible(page: Page, permissions, navigation, plugins_enabled) -> bool:
    if page.any_of and not any(permission in permissions for permission in page.any_of):
        return False
    if page.navigation and not any(navigation.get(key) for key in page.navigation):
        return False
    return True


def _score(needle: str, words: tuple[str, ...], label: str, haystack: tuple[str, ...]) -> int:
    if not words:
        return 1
    if needle and needle in label.lower():
        return 4
    if needle and any(needle in text.lower() for text in haystack):
        return 3
    texts = {word for text in (*haystack, label) for word in _words(text)}
    if all(word in texts for word in words):
        return 2
    return 0


def search(words: str, *, permissions, navigation, plugins_enabled=False, limit=20):
    """Pages and settings matching `words` that this person may open, best first.

    An empty `words` lists every page this person may open (the search box before
    typing). Settings appear only for a typed search, and only when their home page
    may be opened — a field never shows through a forbidden page.
    """
    needle = ' '.join(words.split()).lower()
    wanted = _words(words)
    pages = [page for page in PAGES if _visible(page, permissions, navigation, plugins_enabled)]
    found = []
    for page in pages:
        score = _score(needle, wanted, page.label, (*page.synonyms, page.area_label, page.group or ''))
        if score:
            found.append((score, 0, page.label.lower(), {
                'kind': 'page', 'label': page.label, 'name': None, 'address': page.address,
                'place': page.place, 'scope': page.area_label, 'command': None}))
    if wanted:
        allowed = {page.address for page in pages}
        for entry in setting_entries():
            if entry.address is not None and entry.address not in allowed:
                continue
            if entry.address is None and 'settings:read' not in permissions:
                continue
            score = _score(needle, wanted, entry.label,
                           (*entry.synonyms, entry.name, entry.name.replace('_', ' '), entry.place))
            if score:
                found.append((score, 1, entry.label.lower(), {
                    'kind': 'setting', 'label': entry.label, 'name': entry.name,
                    'address': entry.address, 'place': entry.place, 'scope': entry.scope,
                    'command': entry.command}))
    found.sort(key=lambda item: (-item[0], item[1], item[2]))
    return [item[3] for item in found[:limit]]
