// The console's one navigation table: eight areas, each with its pages. Every
// page has a label, an icon, who may see it and the screen it shows, and an
// address of its own (#/<area>/<page>) so Back, Forward, reload and links work.
// Visibility here is a display hint only; the server checks every request again.
import type { ReactElement, ReactNode } from 'react';
import { Alert, Box, Typography } from '@mui/material';
import FolderCopyIcon from '@mui/icons-material/FolderCopy';
import DashboardIcon from '@mui/icons-material/Dashboard';
import FaxIcon from '@mui/icons-material/Fax';
import InboxIcon from '@mui/icons-material/Inbox';
import ListAltIcon from '@mui/icons-material/ListAlt';
import SendIcon from '@mui/icons-material/Send';
import DialpadIcon from '@mui/icons-material/Dialpad';
import MoveToInboxIcon from '@mui/icons-material/MoveToInbox';
import EmailIcon from '@mui/icons-material/Email';
import AllInboxIcon from '@mui/icons-material/AllInbox';
import ContactPhoneIcon from '@mui/icons-material/ContactPhone';
import ContactsIcon from '@mui/icons-material/Contacts';
import HandshakeIcon from '@mui/icons-material/Handshake';
import CloudIcon from '@mui/icons-material/Cloud';
import SwapHorizIcon from '@mui/icons-material/SwapHoriz';
import SettingsPhoneIcon from '@mui/icons-material/SettingsPhone';
import PaidIcon from '@mui/icons-material/Paid';
import ReceiptLongIcon from '@mui/icons-material/ReceiptLong';
import PriceChangeIcon from '@mui/icons-material/PriceChange';
import SavingsIcon from '@mui/icons-material/Savings';
import LightbulbIcon from '@mui/icons-material/Lightbulb';
import LockOpenIcon from '@mui/icons-material/LockOpen';
import PersonIcon from '@mui/icons-material/Person';
import GroupsIcon from '@mui/icons-material/Groups';
import BadgeIcon from '@mui/icons-material/Badge';
import VpnKeyIcon from '@mui/icons-material/VpnKey';
import DevicesIcon from '@mui/icons-material/Devices';
import SettingsIcon from '@mui/icons-material/Settings';
import HelpIcon from '@mui/icons-material/Help';
import SecurityIcon from '@mui/icons-material/Security';
import StorageIcon from '@mui/icons-material/Storage';
import AssessmentIcon from '@mui/icons-material/Assessment';
import DescriptionIcon from '@mui/icons-material/Description';
import FactCheckIcon from '@mui/icons-material/FactCheck';
import ApiIcon from '@mui/icons-material/Api';
import SmartToyIcon from '@mui/icons-material/SmartToy';
import TerminalIcon from '@mui/icons-material/Terminal';
import ScienceIcon from '@mui/icons-material/Science';
import ExtensionIcon from '@mui/icons-material/Extension';
import AddCircleOutlineIcon from '@mui/icons-material/AddCircleOutline';
import type AdminAPIClient from './api/client';
import type { AdminConfig, AuthMe, ConsoleContext } from './api/types';
import { providerLabel } from './providerLabels';
import Dashboard from './components/Dashboard';
import SetupWizard from './components/SetupWizard';
import JobsList from './components/JobsList';
import Plugins from './components/Plugins';
import ApiKeys from './components/ApiKeys';
import Settings, { type SettingsSection } from './components/Settings';
import Diagnostics from './components/Diagnostics';
import MCP from './components/MCP';
import Logs from './components/Logs';
import SendFax from './components/SendFax';
import Received, { readFilter } from './components/Received';
import ReceivingAddresses from './components/delivery/ReceivingAddresses';
import Connectors from './components/delivery/Connectors';
import ProvidersInUse from './components/ProvidersInUse';
import ProviderRules, { type NumberRuleSummary } from './components/ProviderRules';
import ProviderAccounts from './components/ProviderAccounts';
import { MailboxSendingRulesPicker } from './components/MailboxSendingRules';
import { rulesApiFor } from './components/ProviderRulesApi';
import { currencyFor } from './components/ProviderRulesText';
import AltRouteIcon from '@mui/icons-material/AltRoute';
import CasePackets from './components/delivery/CasePackets';
import Savings from './components/delivery/Savings';
import Recommendations from './components/delivery/Recommendations';
import WorkSettingsPanel from './components/work/WorkSettingsPanel';
import Terminal from './components/Terminal';
import AuditLog from './components/AuditLog';
import { DeploymentSection } from './components/common/Deployment';
import ScriptsTests from './components/ScriptsTests';
import Users from './components/Users';
import Groups from './components/Groups';
import Roles from './components/Roles';
import ResourceAccess from './components/ResourceAccess';
import Sessions from './components/Sessions';
import DeliveryRoutes from './components/DeliveryRoutes';
import DeveloperOverview, { AssistantsOverview } from './components/DeveloperOverview';
import ReplyNumber from './components/ReplyNumber';
import BlockedSenders from './components/BlockedSenders';
import NpiRecordPanel from './components/NpiRecord';
import BlockIcon from '@mui/icons-material/Block';
import Forms from './components/forms/Forms';

export type AreaId = 'overview' | 'faxes' | 'numbers' | 'recipients' | 'providers' | 'costs' | 'access' | 'system';

// Destinations the screens already used before the console had addresses.
// They keep working; each opens the page that now does that job.
export type LegacyDestination = 'send' | 'jobs' | 'inbox' | 'settings' | 'keys' | 'diagnostics' | 'routes' | 'email' | 'trunk' | 'setup';

// A legacy name, or an address without the leading '#/', such as 'recipients/partners'.
export type AdminDestination = LegacyDestination | `${AreaId}/${string}`;

export const LEGACY_DESTINATIONS: Record<LegacyDestination, string> = {
  send: 'faxes/send',
  jobs: 'faxes/sent',
  inbox: 'faxes/received',
  settings: 'providers/sending',
  keys: 'access/keys',
  diagnostics: 'system/diagnostics',
  routes: 'costs/spending',
  email: 'numbers/email',
  trunk: 'providers/trunk',
  setup: 'system/setup',
};

type NavigationKey = 'send' | 'jobs' | 'inbox' | 'work';

// Addresses that moved: the old Work page is Received, showing faxes waiting for an owner.
export const MOVED_ADDRESSES: Record<string, string> = {
  'faxes/work': 'faxes/received?show=waiting',
};

// Who may see a page. A page is visible when every stated condition holds;
// a page with no condition is visible to everyone signed in.
export interface Gate {
  // Holds at least one of these permissions at the installation.
  anyOf?: readonly string[];
  // The server says this person may use this kind of screen (their own faxes count);
  // with several, any one is enough.
  navigation?: NavigationKey | NavigationKey[];
  // Provider plugins are turned on for this installation.
  plugins?: true;
}

export interface PageContext {
  client: AdminAPIClient;
  me: AuthMe;
  permissions: ReadonlySet<string>;
  context: ConsoleContext;
  adminConfig: AdminConfig | null;
  // The console context is being read again for this page, or that failed.
  contextLoading: boolean;
  contextError: string | null;
  docsBase?: string;
  canSetUp: boolean;
  // The address's query, such as ?mine=1 on Keys & phones.
  params: URLSearchParams;
  navigate: (destination: AdminDestination) => void;
  goHome: () => void;
  // A fax Send just queued, opened in Sent when the person follows it.
  jobToOpen: string | null;
  openJob: (jobId: string) => void;
  jobOpened: () => void;
}

export interface NavPage {
  id: string;
  label: string;
  // A name that depends on the installation, such as the trunk's carrier ("Telnyx").
  labelFor?: () => string;
  icon: ReactElement;
  // The provider this page sets up; it is listed in the panel only while that provider is in use.
  provider?: string;
  // An entry that opens another page, such as Add or change a provider (the Setup wizard).
  link?: AdminDestination;
  // False for a page that keeps its address but is not listed in the panel.
  inPanel?: boolean;
  gate: Gate;
  // Pages listed together under a heading inside their area, such as Developer.
  group?: string;
  // The page reads the console context again on entry (active settings it shows).
  refreshContext?: boolean;
  render: (ctx: PageContext) => ReactNode;
}

export interface NavArea {
  id: AreaId;
  label: string;
  icon: ReactElement;
  pages: NavPage[];
}

const SETTINGS_READ = ['settings:read'] as const;

// Loaders the rules screens keep between renders: one per console API client.
const numberRuleLoaders = new WeakMap<AdminAPIClient, () => Promise<NumberRuleSummary[]>>();
const mailboxLoaders = new WeakMap<AdminAPIClient, () => Promise<Array<{ id: string; label: string }>>>();

function numberRules(client: AdminAPIClient): () => Promise<NumberRuleSummary[]> {
  let load = numberRuleLoaders.get(client);
  if (!load) {
    load = () => client.listInboundRules().then((page) => page.items.map((rule) => ({ to_number: rule.to_number, mailbox_label: rule.mailbox_label })));
    numberRuleLoaders.set(client, load);
  }
  return load;
}

function mailboxes(client: AdminAPIClient): () => Promise<Array<{ id: string; label: string }>> {
  let load = mailboxLoaders.get(client);
  if (!load) {
    load = () => client.listMailboxes().then((page) => page.items.map((item) => ({ id: item.id, label: item.label })));
    mailboxLoaders.set(client, load);
  }
  return load;
}

const currency = (ctx: PageContext) => currencyFor(ctx.context.send?.default_country);
const OVERVIEW_GATE: Gate = { anyOf: ['diagnostics:read', 'settings:read'] };

// Pages that show active settings wait for the console context they just asked for.
function whenContextReady(ctx: PageContext, node: ReactNode): ReactNode {
  if (ctx.contextLoading) return <Alert severity="info">Loading…</Alert>;
  if (ctx.contextError) return <Alert severity="error">{ctx.contextError}</Alert>;
  return node;
}

// The owner (or the installation key) may change every setting; others see owner-only ones disabled.
function isOwner(ctx: PageContext): boolean {
  return Boolean(ctx.me.is_owner) || ctx.me.principal.kind === 'bootstrap';
}

function settingsPage(sections: SettingsSection[], title?: string) {
  return (ctx: PageContext) => (
    <Settings client={ctx.client} canWrite={ctx.permissions.has('settings:write')} canRestart={ctx.permissions.has('host:restart')}
      sections={sections} title={title} isOwner={isOwner(ctx)} />
  );
}

// Send a fax, from the pages that offer it, for people who may send.
function sendFax(ctx: PageContext): (() => void) | undefined {
  return ctx.context.navigation.send ? () => ctx.navigate('faxes/send') : undefined;
}

function providerPage(id: string, label: string, section: SettingsSection, provider: string,
  icon: ReactElement = <CloudIcon />): NavPage {
  return { id, label, icon, provider, gate: { anyOf: SETTINGS_READ }, render: settingsPage([section], label) };
}

export const NAVIGATION: NavArea[] = [
  {
    id: 'overview', label: 'Overview', icon: <DashboardIcon />,
    pages: [
      { id: 'overview', label: 'Overview', icon: <DashboardIcon />, gate: OVERVIEW_GATE,
        render: (ctx) => <Dashboard client={ctx.client} onNavigate={ctx.navigate} canSetUp={ctx.canSetUp} onSendFax={sendFax(ctx)} /> },
    ],
  },
  {
    id: 'faxes', label: 'Faxes', icon: <FaxIcon />,
    pages: [
      { id: 'received', label: 'Received', icon: <InboxIcon />, gate: { navigation: ['inbox', 'work'] }, refreshContext: true,
        render: (ctx) => whenContextReady(ctx, <Received client={ctx.client} inboundEnabled={ctx.context.inbound_enabled ?? undefined}
          onNavigate={ctx.navigate} docsBase={ctx.docsBase} permissions={ctx.permissions}
          canList={ctx.context.navigation.inbox} canWork={Boolean(ctx.context.navigation.work)}
          show={readFilter(ctx.params.get('show'))}
          onShowChange={(next) => ctx.navigate(next === 'all' ? 'faxes/received' : `faxes/received?show=${next}`)}
          onSendFax={sendFax(ctx)} />) },
      { id: 'sent', label: 'Sent', icon: <ListAltIcon />, gate: { navigation: 'jobs' },
        render: (ctx) => <JobsList client={ctx.client} openJobId={ctx.jobToOpen} onOpened={ctx.jobOpened} onSendFax={sendFax(ctx)}
          canApprove={ctx.permissions.has('fax:approve')} onNavigate={ctx.navigate} /> },
      { id: 'send', label: 'Send a fax', icon: <SendIcon />, gate: { navigation: 'send' }, refreshContext: true,
        render: (ctx) => <SendFax client={ctx.client} config={ctx.adminConfig} configLoading={ctx.contextLoading}
          configError={ctx.contextError} onOpenJob={ctx.openJob} sendChoices={ctx.context.send} /> },
      // Registered forms: import, fill in and send; partners get only the values (faxbot forms).
      // Reading forms needs settings:read or fax:send, so a fax operator can fill one in and send it.
      { id: 'forms', label: 'Forms', icon: <DescriptionIcon />, gate: { anyOf: ['settings:read', 'fax:send'] },
        render: (ctx) => <Forms client={ctx.client} canWrite={ctx.permissions.has('settings:write')}
          canSend={Boolean(ctx.context.navigation.send)} canReadSettings={ctx.permissions.has('settings:read')} /> },
    ],
  },
  {
    id: 'numbers', label: 'Numbers', icon: <DialpadIcon />,
    pages: [
      { id: 'list', label: 'Your numbers', icon: <DialpadIcon />, gate: { anyOf: ['mailboxes:read', 'mailboxes:manage'] },
        render: (ctx) => <ResourceAccess client={ctx.client} me={ctx.me} section="numbers" onNavigate={ctx.navigate} /> },
      // Acknowledgement targets are settings, so people who read settings see them here too.
      { id: 'mailboxes', label: 'Mailboxes', icon: <MoveToInboxIcon />, gate: { anyOf: ['mailboxes:read', 'mailboxes:manage', 'settings:read'] },
        render: (ctx) => (
          <>
            {(ctx.permissions.has('mailboxes:read') || ctx.permissions.has('mailboxes:manage'))
              && <ResourceAccess client={ctx.client} me={ctx.me} section="mailboxes" />}
            {(ctx.permissions.has('mailboxes:read') || ctx.permissions.has('mailboxes:manage')) && (
              <Box sx={{ mt: 4 }}>
                <MailboxSendingRulesPicker api={rulesApiFor(ctx.client)} loadMailboxes={mailboxes(ctx.client)}
                  canWrite={ctx.permissions.has('mailboxes:manage')} currency={currency(ctx)} />
              </Box>
            )}
            {ctx.permissions.has('settings:read') && (
              <Box sx={{ mt: 4 }}><WorkSettingsPanel client={ctx.client} canWrite={ctx.permissions.has('settings:write')} /></Box>
            )}
          </>
        ) },
      { id: 'email', label: 'Email delivery', icon: <EmailIcon />, gate: { anyOf: SETTINGS_READ },
        render: settingsPage(['intake', 'email'], 'Email delivery') },
      // The organization's NPIs and what the NPI registry lists for them (routing/nppes.py).
      { id: 'npi', label: 'Your NPI record', icon: <FactCheckIcon />, gate: { anyOf: SETTINGS_READ },
        render: (ctx) => <NpiRecordPanel client={ctx.client} canWrite={ctx.permissions.has('settings:write')} /> },
      { id: 'blocked', label: 'Blocked senders', icon: <BlockIcon />, gate: { anyOf: SETTINGS_READ },
        render: (ctx) => <BlockedSenders client={ctx.client} canWrite={ctx.permissions.has('settings:write')} /> },
      // Mailboxes and folders that bring documents in or send faxes (intake connectors).
      { id: 'connectors', label: 'Email and folders', icon: <AllInboxIcon />, gate: { anyOf: SETTINGS_READ },
        render: (ctx) => <Connectors client={ctx.client} canWrite={ctx.permissions.has('settings:write')} /> },
      { id: 'identity', label: 'Sender identity', icon: <BadgeIcon />, gate: { anyOf: SETTINGS_READ },
        render: (ctx) => (
          <>
            {settingsPage(['identity'], 'Sender identity')(ctx)}
            <ReplyNumber client={ctx.client} canWrite={ctx.permissions.has('settings:write')} />
          </>
        ) },
    ],
  },
  {
    id: 'recipients', label: 'Recipients', icon: <ContactsIcon />,
    pages: [
      { id: 'list', label: 'Recipients', icon: <ContactPhoneIcon />, gate: { anyOf: SETTINGS_READ },
        render: (ctx) => <DeliveryRoutes client={ctx.client} canWrite={ctx.permissions.has('settings:write')} section="numbers" /> },
      { id: 'partners', label: 'Partners', icon: <HandshakeIcon />, gate: { anyOf: SETTINGS_READ },
        render: (ctx) => (
          <>
            <DeliveryRoutes client={ctx.client} canWrite={ctx.permissions.has('settings:write')} section="partners" />
            {settingsPage(['direct'])(ctx)}
          </>
        ) },
      { id: 'cases', label: 'Case packets', icon: <FolderCopyIcon />, gate: { anyOf: SETTINGS_READ },
        render: (ctx) => <CasePackets client={ctx.client} canSend={ctx.context.navigation.send}
          canWrite={ctx.permissions.has('settings:write')} onNavigate={ctx.navigate} /> },
    ],
  },
  {
    id: 'providers', label: 'Providers', icon: <CloudIcon />,
    pages: [
      { id: 'sending', label: 'In use', icon: <SwapHorizIcon />, gate: { anyOf: SETTINGS_READ }, refreshContext: true,
        render: (ctx) => whenContextReady(ctx,
          <>
            <Typography variant="h4" component="h1" sx={{ mb: 2 }}>In use</Typography>
            <ProvidersInUse context={ctx.context} canChange={ctx.permissions.has('settings:write')} onNavigate={ctx.navigate} />
            <ProviderAccounts api={rulesApiFor(ctx.client)} canWrite={ctx.permissions.has('settings:write')}
              currency={currency(ctx)} onNavigate={ctx.navigate} />
            {settingsPage(['providers', 'inbound', 'routes'])(ctx)}
            {ctx.permissions.has('providers:read') && <ReceivingAddresses client={ctx.client} />}
          </>) },
      // Which account sends each fax: the organization's rules, sites, lists and workflows.
      { id: 'rules', label: 'Rules', icon: <AltRouteIcon />, gate: { anyOf: SETTINGS_READ },
        render: (ctx) => <ProviderRules api={rulesApiFor(ctx.client)} canWrite={ctx.permissions.has('settings:write')}
          currency={currency(ctx)} onNavigate={ctx.navigate}
          loadNumberRules={ctx.permissions.has('mailboxes:read') || ctx.permissions.has('mailboxes:manage') ? numberRules(ctx.client) : undefined} /> },
      providerPage('humblefax', 'HumbleFax', 'humblefax', 'humblefax'),
      providerPage('efax', 'eFax', 'efax', 'efax'),
      providerPage('phaxio', 'Phaxio', 'phaxio', 'phaxio'),
      providerPage('sinch', 'Sinch', 'sinch', 'sinch'),
      providerPage('signalwire', 'SignalWire', 'signalwire', 'signalwire'),
      providerPage('documo', 'Documo', 'documo', 'documo'),
      // Titled by its carrier or phone system ("Telnyx", "Avaya IP Office").
      { id: 'trunk', label: 'Carrier trunk', labelFor: () => providerLabel('sip'), icon: <SettingsPhoneIcon />, provider: 'sip',
        gate: { anyOf: SETTINGS_READ }, render: (ctx) => settingsPage(['trunk'], providerLabel('sip'))(ctx) },
      // Choosing or changing providers happens in the Setup wizard.
      { id: 'change', label: 'Add or change a provider', icon: <AddCircleOutlineIcon />, link: 'system/setup',
        gate: { anyOf: ['settings:write'] }, render: () => null },
    ],
  },
  {
    id: 'costs', label: 'Costs', icon: <PaidIcon />,
    pages: [
      { id: 'spending', label: 'Spending', icon: <ReceiptLongIcon />, gate: { anyOf: SETTINGS_READ },
        render: (ctx) => <DeliveryRoutes client={ctx.client} canWrite={ctx.permissions.has('settings:write')} section="spending" /> },
      { id: 'prices', label: 'Prices & plans', icon: <PriceChangeIcon />, gate: { anyOf: SETTINGS_READ },
        render: (ctx) => <DeliveryRoutes client={ctx.client} canWrite={ctx.permissions.has('settings:write')} section="rates" /> },
      { id: 'savings', label: 'Savings', icon: <SavingsIcon />, gate: { anyOf: SETTINGS_READ },
        render: (ctx) => <Savings client={ctx.client} /> },
      { id: 'recommendations', label: 'Recommendations', icon: <LightbulbIcon />, gate: { anyOf: SETTINGS_READ },
        render: (ctx) => <Recommendations client={ctx.client} canWrite={ctx.permissions.has('settings:write')}
          onNavigate={ctx.navigate} /> },
    ],
  },
  {
    id: 'access', label: 'Access', icon: <LockOpenIcon />,
    pages: [
      { id: 'users', label: 'Users', icon: <PersonIcon />, gate: { anyOf: ['users:read', 'users:manage'] },
        render: (ctx) => <Users client={ctx.client} me={ctx.me} /> },
      { id: 'groups', label: 'Groups', icon: <GroupsIcon />, gate: { anyOf: ['groups:read', 'groups:manage'] },
        render: (ctx) => <Groups client={ctx.client} me={ctx.me} /> },
      { id: 'roles', label: 'Roles', icon: <BadgeIcon />, gate: { anyOf: ['roles:read', 'roles:manage'] },
        render: (ctx) => <Roles client={ctx.client} me={ctx.me} /> },
      { id: 'who', label: 'Who has access', icon: <LockOpenIcon />, gate: { anyOf: ['grants:read', 'grants:manage'] },
        render: (ctx) => <ResourceAccess client={ctx.client} me={ctx.me} section="assignments" /> },
      { id: 'keys', label: 'Keys & phones', icon: <VpnKeyIcon />, gate: { anyOf: ['keys:manage'] },
        render: (ctx) => (
          <>
            <ApiKeys client={ctx.client} me={ctx.me} onlyMine={ctx.params.get('mine') === '1'}
              onShowAll={() => ctx.navigate('access/keys')} />
            {ctx.permissions.has('settings:read') && <Box sx={{ mt: 4 }}>{settingsPage(['installation-key', 'phones'])(ctx)}</Box>}
          </>
        ) },
      // Everyone can see and end their own sessions.
      { id: 'sessions', label: 'Sessions', icon: <DevicesIcon />, gate: {},
        render: (ctx) => <Sessions client={ctx.client} me={ctx.me} /> },
    ],
  },
  {
    id: 'system', label: 'System', icon: <SettingsIcon />,
    pages: [
      { id: 'setup', label: 'Setup', icon: <HelpIcon />, gate: { anyOf: ['settings:write'] },
        render: (ctx) => <SetupWizard client={ctx.client} onDone={ctx.goHome} docsBase={ctx.docsBase} canRestart={ctx.permissions.has('host:restart')}
          isOwner={isOwner(ctx)} /> },
      { id: 'security', label: 'Security', icon: <SecurityIcon />, gate: { anyOf: SETTINGS_READ },
        render: settingsPage(['security'], 'Security') },
      { id: 'storage', label: 'Storage & retention', icon: <StorageIcon />, gate: { anyOf: SETTINGS_READ },
        render: settingsPage(['storage', 'advanced', 'backup'], 'Storage & retention') },
      { id: 'audit', label: 'Audit log', icon: <FactCheckIcon />, gate: { anyOf: ['audit:read'] },
        render: (ctx) => (
          <>
            <AuditLog client={ctx.client} canListPeople={ctx.permissions.has('users:read')} />
            {ctx.permissions.has('settings:read') && <Box sx={{ mt: 4 }}>{settingsPage(['audit'])(ctx)}</Box>}
          </>
        ) },
      { id: 'diagnostics', label: 'Diagnostics', icon: <AssessmentIcon />, gate: { anyOf: ['diagnostics:read'] }, refreshContext: true,
        render: (ctx) => whenContextReady(ctx,
          <>
            <Diagnostics client={ctx.client} onNavigate={ctx.navigate} docsBase={ctx.docsBase} />
            {ctx.permissions.has('settings:read') && <Box sx={{ mt: 4 }}>{settingsPage(['diagnostics'])(ctx)}</Box>}
          </>) },
      { id: 'logs', label: 'Logs', icon: <DescriptionIcon />, gate: { anyOf: ['logs:read'] },
        render: (ctx) => <Logs client={ctx.client} /> },
      { id: 'api', label: 'API & SDKs', icon: <ApiIcon />, gate: OVERVIEW_GATE, group: 'Developer',
        render: (ctx) => (
          <>
            <DeveloperOverview client={ctx.client} />
            {ctx.permissions.has('settings:read') && <Box sx={{ mt: 4 }}>{settingsPage(['developer'])(ctx)}</Box>}
          </>
        ) },
      { id: 'assistants', label: 'AI assistants', icon: <SmartToyIcon />, gate: { anyOf: SETTINGS_READ }, group: 'Developer',
        render: (ctx) => (
          <>
            <AssistantsOverview client={ctx.client} />
            <MCP client={ctx.client} />
            {settingsPage(['mcp'])(ctx)}
          </>
        ) },
      { id: 'terminal', label: 'Terminal', icon: <TerminalIcon />, gate: { anyOf: ['host:terminal'] }, group: 'Developer',
        render: (ctx) => (
          <>
            <Terminal client={ctx.client} />
            <DeploymentSection client={ctx.client} names={['ENABLE_ADMIN_EXEC']} title="Terminal setting" showNames />
          </>
        ) },
      { id: 'scripts', label: 'Scripts & checks', icon: <ScienceIcon />, gate: { anyOf: ['providers:write'] }, group: 'Developer',
        refreshContext: true,
        render: (ctx) => whenContextReady(ctx, <ScriptsTests client={ctx.client} onNavigate={ctx.navigate} docsBase={ctx.docsBase} />) },
      // Always listed, so plugins can be turned on here; the installed plugins show once they are on.
      { id: 'plugins', label: 'Provider plugins', icon: <ExtensionIcon />, gate: { anyOf: ['providers:read', 'settings:read'] },
        group: 'Developer', refreshContext: true,
        render: (ctx) => whenContextReady(ctx,
          <>
            {ctx.permissions.has('settings:read') && settingsPage(['plugins'], 'Provider plugins')(ctx)}
            {ctx.context.provider_view?.plugins_enabled && ctx.permissions.has('providers:read') && (
              <Box sx={{ mt: 4 }}>
                <Plugins client={ctx.client} config={ctx.adminConfig} configLoading={ctx.contextLoading}
                  configError={ctx.contextError} onNavigate={ctx.navigate} />
              </Box>
            )}
          </>) },
    ],
  },
];

export type ConsoleNavigation = ConsoleContext['navigation'];

export function pageVisible(gate: Gate, permissions: ReadonlySet<string>, navigation: ConsoleNavigation,
  options: { pluginsEnabled: boolean }): boolean {
  if (gate.anyOf && !gate.anyOf.some((permission) => permissions.has(permission))) return false;
  if (gate.navigation && ![gate.navigation].flat().some((key) => navigation[key])) return false;
  if (gate.plugins && !options.pluginsEnabled) return false;
  return true;
}

// The areas and pages this person may see, in menu order; an area with no
// visible page is left out.
// Providers this installation uses (sending, receiving, extra routes); null when this person may not see them.
export function providersInUse(context: Pick<ConsoleContext, 'provider_view'>): string[] | null {
  const view = context.provider_view;
  if (!view) return null;
  return [...new Set([view.active_outbound, view.active_inbound, ...(view.extra_routes ?? [])].filter(Boolean))];
}

export function visibleNavigation(permissions: ReadonlySet<string>, navigation: ConsoleNavigation,
  options: { pluginsEnabled: boolean; providersInUse?: string[] | null }): NavArea[] {
  // A provider page leaves the panel while its provider is not in use; its address still opens it.
  const inUse = options.providersInUse ?? null;
  const shown = (page: NavPage): NavPage => ({
    ...page,
    label: page.labelFor ? page.labelFor() : page.label,
    inPanel: !page.provider || inUse === null || inUse.includes(page.provider),
  });
  return NAVIGATION
    .map((area) => ({ ...area, pages: area.pages.filter((page) => pageVisible(page.gate, permissions, navigation, options)).map(shown) }))
    .filter((area) => area.pages.length > 0);
}

// An area with one page (Overview) has the short address #/overview.
function singlePage(areaId: string): boolean {
  return NAVIGATION.find((area) => area.id === areaId)?.pages.length === 1;
}

export function pageAddress(areaId: string, pageId?: string, params?: URLSearchParams | string): string {
  const query = params ? String(params) : '';
  const path = !pageId || singlePage(areaId) ? `#/${areaId}` : `#/${areaId}/${pageId}`;
  return query ? `${path}?${query}` : path;
}

export interface ParsedAddress {
  area: string;
  page: string | null;
  params: URLSearchParams;
}

// '#/providers/trunk' → providers, trunk. Anything else is not an address.
export function parseAddress(hash: string): ParsedAddress | null {
  const match = /^#\/([a-z]+)(?:\/([a-z-]+))?\/?(?:\?(.*))?$/.exec(hash);
  if (!match) return null;
  return { area: match[1], page: match[2] ?? null, params: new URLSearchParams(match[3] ?? '') };
}

export interface ResolvedPage {
  area: NavArea;
  page: NavPage;
  // The address this page is shown at; differs from the requested one after a fallback.
  address: string;
}

// The page for an address among the visible ones: the area's first page when
// only the area is named, and the first visible page when the address is
// unknown or this person may not see it.
export function resolveAddress(visible: NavArea[], requested: ParsedAddress | null): ResolvedPage | null {
  if (visible.length === 0) return null;
  const moved = requested?.page ? MOVED_ADDRESSES[`${requested.area}/${requested.page}`] : undefined;
  const parsed = moved ? parseAddress(`#/${moved}`) : requested;
  const area = parsed ? visible.find((candidate) => candidate.id === parsed.area) : undefined;
  if (area) {
    const requested = parsed?.page ? area.pages.find((page) => page.id === parsed.page) : undefined;
    // An entry that opens another page, such as Add or change a provider.
    if (requested?.link) return resolveAddress(visible, parseAddress(destinationAddress(requested.link)));
    if (requested || !parsed?.page || singlePage(area.id)) {
      const page = requested ?? area.pages[0];
      const keepParams = requested || singlePage(area.id);
      return { area, page, address: pageAddress(area.id, page.id, keepParams ? parsed?.params : undefined) };
    }
  }
  const first = visible[0];
  return { area: first, page: first.pages[0], address: pageAddress(first.id, first.pages[0].id) };
}

// The address part of a destination: a legacy name through LEGACY_DESTINATIONS,
// or 'area/page' (with an optional ?query) as given.
export function destinationAddress(destination: AdminDestination): string {
  const path = (LEGACY_DESTINATIONS as Record<string, string>)[destination] ?? destination;
  const [route, query] = path.split('?');
  const [area, page] = route.split('/');
  return pageAddress(area, page, query);
}
