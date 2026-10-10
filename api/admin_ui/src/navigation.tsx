// The console's one navigation table: six areas, each with its pages, some under
// a group heading. Every page has a label, an icon, who may see it and the screen
// it shows, and an address of its own (#/<area>/<page>) so Back, Forward, reload
// and links work. Every address the console had before the six areas still opens
// the page that now does that job (MOVED_ADDRESSES). Visibility here is a display
// hint only; the server checks every request again.
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
import ReceiptIcon from '@mui/icons-material/Receipt';
import RequestQuoteIcon from '@mui/icons-material/RequestQuote';
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
import AIAnalysis from './components/AIAnalysis';
import SetupWizard from './components/SetupWizard';
import JobsList, { readSentSince, readSentStatus } from './components/JobsList';
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
import DigitalAccounts from './components/DigitalAccounts';
import { DigitalReceived } from './components/delivery/DigitalMessages';
import { MailboxSendingRulesPicker } from './components/MailboxSendingRules';
import { rulesApiFor } from './components/ProviderRulesApi';
import { currencyFor } from './components/ProviderRulesText';
import AltRouteIcon from '@mui/icons-material/AltRoute';
import CasePackets from './components/delivery/CasePackets';
import Savings from './components/delivery/Savings';
import Charges from './components/delivery/Charges';
import Invoices from './components/delivery/Invoices';
import Recommendations from './components/delivery/Recommendations';
import FactAdvice from './components/delivery/FactAdvice';
import WorkSettingsPanel from './components/work/WorkSettingsPanel';
import UncertainSettingsPanel from './components/work/UncertainSettingsPanel';
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
import NumberMoves from './components/NumberMoves';
import BlockIcon from '@mui/icons-material/Block';
import Forms from './components/forms/Forms';
import ExpectedFaxes, { readExpectedView } from './components/expected/ExpectedFaxes';
import PendingActionsIcon from '@mui/icons-material/PendingActions';
import TuneIcon from '@mui/icons-material/Tune';
import AutoAwesomeIcon from '@mui/icons-material/AutoAwesome';
import Capabilities from './components/capabilities/Capabilities';

export type AreaId = 'overview' | 'savings' | 'faxes' | 'delivery' | 'recipients' | 'admin';

// The areas the console had before the six. Their addresses still work: each opens
// the page that now does that job.
export type FormerAreaId = 'numbers' | 'providers' | 'costs' | 'access' | 'system';

// Destinations the screens already used before the console had addresses.
// They keep working; each opens the page that now does that job.
export type LegacyDestination = 'send' | 'jobs' | 'inbox' | 'settings' | 'keys' | 'diagnostics' | 'routes' | 'email' | 'trunk' | 'setup';

// A legacy name, or an address without the leading '#/', such as 'recipients/partners'.
// An address in a former area ('costs/savings?part=sslfax') opens its new home.
export type AdminDestination = LegacyDestination | `${AreaId | FormerAreaId}/${string}`;

export const LEGACY_DESTINATIONS: Record<LegacyDestination, string> = {
  send: 'faxes/send',
  jobs: 'faxes/sent',
  inbox: 'faxes/received',
  settings: 'delivery/connections',
  keys: 'admin/keys',
  diagnostics: 'admin/health',
  routes: 'savings/spending',
  email: 'delivery/email',
  trunk: 'delivery/trunk',
  setup: 'admin/setup',
};

type NavigationKey = 'send' | 'jobs' | 'inbox' | 'work';

// Every address that moved, the page that does its job now, and where it was in the
// menu (for the one-line notice an old link shows). A fixed query on the new address
// (show=waiting) comes first; the old address keeps its own query after it.
const MOVES: ReadonlyArray<readonly [from: string, to: string, was: string]> = [
  ['faxes/work', 'faxes/received?show=waiting', 'Faxes › Work'],
  ['numbers/list', 'delivery/numbers', 'Numbers › Your numbers'],
  ['numbers/mailboxes', 'delivery/mailboxes', 'Numbers › Mailboxes'],
  ['numbers/email', 'delivery/email', 'Numbers › Email delivery'],
  ['numbers/advice', 'delivery/moves', 'Numbers › Advice and moves'],
  ['numbers/npi', 'admin/npi', 'Numbers › Your NPI record'],
  ['numbers/blocked', 'delivery/blocked', 'Numbers › Blocked senders'],
  ['numbers/connectors', 'delivery/connectors', 'Numbers › Email and folders'],
  ['numbers/identity', 'delivery/identity', 'Numbers › Sender identity'],
  ['recipients/cases', 'faxes/cases', 'Recipients › Case packets'],
  ['providers/sending', 'delivery/connections', 'Providers › In use'],
  ['providers/rules', 'delivery/rules', 'Providers › Rules'],
  ['providers/humblefax', 'delivery/humblefax', 'Providers › HumbleFax'],
  ['providers/efax', 'delivery/efax', 'Providers › eFax'],
  ['providers/phaxio', 'delivery/phaxio', 'Providers › Phaxio'],
  ['providers/sinch', 'delivery/sinch', 'Providers › Sinch'],
  ['providers/signalwire', 'delivery/signalwire', 'Providers › SignalWire'],
  ['providers/documo', 'delivery/documo', 'Providers › Documo'],
  ['providers/trunk', 'delivery/trunk', 'Providers › Carrier trunk'],
  ['providers/change', 'delivery/change', 'Providers › Add or change a provider'],
  ['costs/spending', 'savings/spending', 'Costs › Spending'],
  ['costs/charges', 'savings/charges', 'Costs › Charges'],
  ['costs/invoices', 'savings/invoices', 'Costs › Invoices'],
  ['costs/prices', 'savings/prices', 'Costs › Prices & plans'],
  ['costs/savings', 'savings/results', 'Costs › Savings'],
  ['costs/recommendations', 'savings/opportunities', 'Costs › Recommendations'],
  ['costs/advice', 'savings/facts', 'Costs › Advice'],
  ['access/users', 'admin/users', 'Access › Users'],
  ['access/groups', 'admin/groups', 'Access › Groups'],
  ['access/roles', 'admin/roles', 'Access › Roles'],
  ['access/who', 'admin/who', 'Access › Who has access'],
  ['access/keys', 'admin/keys', 'Access › Keys & phones'],
  ['access/sessions', 'admin/sessions', 'Access › Sessions'],
  ['system/setup', 'admin/setup', 'System › Setup'],
  ['system/analysis', 'admin/analysis', 'System › AI analysis'],
  ['system/security', 'admin/security', 'System › Security'],
  ['system/storage', 'admin/retention', 'System › Storage & retention'],
  ['system/audit', 'admin/audit', 'System › Audit log'],
  ['system/diagnostics', 'admin/health', 'System › Diagnostics'],
  ['system/logs', 'admin/logs', 'System › Logs'],
  ['system/api', 'admin/api', 'System › API & SDKs'],
  ['system/assistants', 'admin/assistants', 'System › AI assistants'],
  ['system/terminal', 'admin/terminal', 'System › Terminal'],
  ['system/scripts', 'admin/scripts', 'System › Scripts & checks'],
  ['system/plugins', 'admin/plugins', 'System › Provider plugins'],
];

// Old address ('costs/savings') → its new home ('savings/results').
export const MOVED_ADDRESSES: Record<string, string> = Object.fromEntries(MOVES.map(([from, to]) => [from, to]));
const MOVED_FROM: Record<string, string> = Object.fromEntries(MOVES.map(([from, , was]) => [from, was]));

// A former area on its own (#/costs) opens the first of its old pages this person may open.
const FORMER_AREAS: Record<FormerAreaId, string> = {
  numbers: 'Numbers', providers: 'Providers', costs: 'Costs', access: 'Access', system: 'System',
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
  return { id, label, icon, provider, group: CONNECTIONS, gate: { anyOf: SETTINGS_READ }, render: settingsPage([section], label) };
}

// Group headings inside an area.
const NUMBERS_AND_MAILBOXES = 'Numbers & mailboxes';
const DOCUMENTS = 'Documents in and out';
const CONNECTIONS = 'Connections';
const PEOPLE = 'People & access';
const INSTALLATION = 'Installation';
const MONITORING = 'Monitoring';
const DEVELOPER = 'Developer';

export const NAVIGATION: NavArea[] = [
  {
    id: 'overview', label: 'Overview', icon: <DashboardIcon />,
    pages: [
      { id: 'overview', label: 'Overview', icon: <DashboardIcon />, gate: OVERVIEW_GATE,
        render: (ctx) => <Dashboard client={ctx.client} onNavigate={ctx.navigate} canSetUp={ctx.canSetUp} onSendFax={sendFax(ctx)}
          canReadSettings={ctx.permissions.has('settings:read')} canReadAnalysis={ctx.permissions.has('settings:read')} /> },
    ],
  },
  {
    id: 'savings', label: 'Savings & optimization', icon: <PaidIcon />,
    pages: [
      // Every way Faxbot can save money or improve delivery, on or off (the mechanism catalogue).
      { id: 'capabilities', label: 'Capabilities', icon: <AutoAwesomeIcon />, gate: { anyOf: SETTINGS_READ },
        render: (ctx) => <Capabilities client={ctx.client} show={ctx.params.get('show')} capability={ctx.params.get('key')}
          onNavigate={ctx.navigate} /> },
      { id: 'opportunities', label: 'Opportunities', icon: <LightbulbIcon />, gate: { anyOf: SETTINGS_READ },
        render: (ctx) => <Recommendations client={ctx.client} canWrite={ctx.permissions.has('settings:write')}
          onNavigate={ctx.navigate} focus={ctx.params.get('section')} /> },
      // What one missing fact (a partner, a recipient's approval, a price, a plan's allowance) cost you.
      { id: 'facts', label: 'Facts to establish', icon: <FactCheckIcon />, gate: { anyOf: SETTINGS_READ },
        render: (ctx) => <FactAdvice client={ctx.client} /> },
      { id: 'results', label: 'Savings results', icon: <SavingsIcon />, gate: { anyOf: SETTINGS_READ },
        render: (ctx) => <Savings client={ctx.client} focus={ctx.params.get('part')} /> },
      { id: 'spending', label: 'Spending', icon: <ReceiptLongIcon />, gate: { anyOf: SETTINGS_READ },
        render: (ctx) => <DeliveryRoutes client={ctx.client} canWrite={ctx.permissions.has('settings:write')} section="spending" /> },
      // What each provider charged, and faxes a provider billed that Faxbot has no record of.
      { id: 'charges', label: 'Charges', icon: <ReceiptIcon />, gate: { anyOf: SETTINGS_READ },
        render: (ctx) => <Charges client={ctx.client} canWrite={ctx.permissions.has('settings:write')} /> },
      // Monthly invoice totals, and the part your faxes don't explain.
      { id: 'invoices', label: 'Invoices', icon: <RequestQuoteIcon />, gate: { anyOf: SETTINGS_READ },
        render: (ctx) => <Invoices client={ctx.client} canWrite={ctx.permissions.has('settings:write')} /> },
      { id: 'prices', label: 'Prices & plans', icon: <PriceChangeIcon />, gate: { anyOf: SETTINGS_READ },
        render: (ctx) => <DeliveryRoutes client={ctx.client} canWrite={ctx.permissions.has('settings:write')} section="rates" /> },
    ],
  },
  {
    id: 'faxes', label: 'Faxes', icon: <FaxIcon />,
    pages: [
      { id: 'received', label: 'Received', icon: <InboxIcon />, gate: { navigation: ['inbox', 'work'] }, refreshContext: true,
        render: (ctx) => whenContextReady(ctx, <>
          <Received client={ctx.client} inboundEnabled={ctx.context.inbound_enabled ?? undefined}
            onNavigate={ctx.navigate} docsBase={ctx.docsBase} permissions={ctx.permissions}
            canList={ctx.context.navigation.inbox} canWork={Boolean(ctx.context.navigation.work)}
            show={readFilter(ctx.params.get('show'))}
            onShowChange={(next) => ctx.navigate(next === 'all' ? 'faxes/received' : `faxes/received?show=${next}`)}
            onSendFax={sendFax(ctx)} onOpenSentFax={ctx.openJob} />
          {ctx.permissions.has('settings:read') && <DigitalReceived client={ctx.client} />}
        </>) },
      { id: 'sent', label: 'Sent', icon: <ListAltIcon />, gate: { navigation: 'jobs' },
        render: (ctx) => <JobsList client={ctx.client} openJobId={ctx.jobToOpen} onOpened={ctx.jobOpened} onSendFax={sendFax(ctx)}
          canApprove={ctx.permissions.has('fax:approve')} onNavigate={ctx.navigate}
          status={readSentStatus(ctx.params.get('status'))} heldOnly={ctx.params.get('show') === 'held'}
          sinceHours={readSentSince(ctx.params.get('since'))}
          onStatusChange={(next) => ctx.navigate(next ? `faxes/sent?status=${next}` : 'faxes/sent')}
          onShowAnyTime={() => {
            const status = readSentStatus(ctx.params.get('status'));
            ctx.navigate(status ? `faxes/sent?status=${status}` : 'faxes/sent');
          }}
          onShowAll={() => ctx.navigate('faxes/sent')} /> },
      // Listed at the top of the panel as a persistent action rather than among the Faxes pages.
      { id: 'send', label: 'Send a fax', icon: <SendIcon />, gate: { navigation: 'send' }, refreshContext: true, inPanel: false,
        render: (ctx) => <SendFax client={ctx.client} config={ctx.adminConfig} configLoading={ctx.contextLoading}
          configError={ctx.contextError} onOpenJob={ctx.openJob} sendChoices={ctx.context.send} /> },
      // Faxes recorded before they arrive, matched by a reference the sender stated; imports of open work and
      // outage recovery (faxbot expected).
      { id: 'expected', label: 'Expected', icon: <PendingActionsIcon />, gate: { anyOf: ['work:read', 'work:import'] },
        render: (ctx) => <ExpectedFaxes client={ctx.client} canImport={ctx.permissions.has('work:import')}
          canOutage={ctx.permissions.has('work:import') || ctx.permissions.has('settings:write')}
          show={readExpectedView(ctx.params.get('show'))}
          onShowChange={(next) => ctx.navigate(next === 'waiting' ? 'faxes/expected' : `faxes/expected?show=${next}`)} /> },
      // Registered forms: import, fill in and send; partners get only the values (faxbot forms).
      // Reading forms needs settings:read or fax:send, so a fax operator can fill one in and send it.
      { id: 'forms', label: 'Forms', icon: <DescriptionIcon />, gate: { anyOf: ['settings:read', 'fax:send'] },
        render: (ctx) => <Forms client={ctx.client} canWrite={ctx.permissions.has('settings:write')}
          canSend={Boolean(ctx.context.navigation.send)} canReadSettings={ctx.permissions.has('settings:read')} /> },
      { id: 'cases', label: 'Case packets', icon: <FolderCopyIcon />, gate: { anyOf: SETTINGS_READ },
        render: (ctx) => <CasePackets client={ctx.client} canSend={ctx.context.navigation.send}
          canWrite={ctx.permissions.has('settings:write')} onNavigate={ctx.navigate} /> },
    ],
  },
  {
    id: 'delivery', label: 'Delivery setup', icon: <TuneIcon />,
    pages: [
      { id: 'numbers', label: 'Numbers', icon: <DialpadIcon />, group: NUMBERS_AND_MAILBOXES,
        gate: { anyOf: ['mailboxes:read', 'mailboxes:manage'] },
        render: (ctx) => <ResourceAccess client={ctx.client} me={ctx.me} section="numbers" onNavigate={ctx.navigate} /> },
      // Acknowledgement targets are settings, so people who read settings see them here too.
      { id: 'mailboxes', label: 'Mailboxes', icon: <MoveToInboxIcon />, group: NUMBERS_AND_MAILBOXES,
        gate: { anyOf: ['mailboxes:read', 'mailboxes:manage', 'settings:read'] },
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
            {ctx.permissions.has('settings:read') && (
              <Box sx={{ mt: 4 }}><UncertainSettingsPanel client={ctx.client} canWrite={ctx.permissions.has('settings:write')} /></Box>
            )}
          </>
        ) },
      { id: 'moves', label: 'Number moves', icon: <SwapHorizIcon />, group: NUMBERS_AND_MAILBOXES, gate: { anyOf: SETTINGS_READ },
        render: (ctx) => <NumberMoves client={ctx.client} canWrite={ctx.permissions.has('settings:write')} /> },
      { id: 'blocked', label: 'Blocked senders', icon: <BlockIcon />, group: NUMBERS_AND_MAILBOXES, gate: { anyOf: SETTINGS_READ },
        render: (ctx) => <BlockedSenders client={ctx.client} canWrite={ctx.permissions.has('settings:write')} /> },
      { id: 'identity', label: 'Sending identity', icon: <BadgeIcon />, group: NUMBERS_AND_MAILBOXES, gate: { anyOf: SETTINGS_READ },
        render: (ctx) => (
          <>
            {settingsPage(['identity'], 'Sending identity')(ctx)}
            <ReplyNumber client={ctx.client} canWrite={ctx.permissions.has('settings:write')} />
          </>
        ) },
      { id: 'email', label: 'Staff email delivery', icon: <EmailIcon />, group: DOCUMENTS, gate: { anyOf: SETTINGS_READ },
        render: settingsPage(['intake', 'email'], 'Staff email delivery') },
      // Mailboxes and folders that bring documents in or send faxes (intake connectors).
      { id: 'connectors', label: 'Email and folders', icon: <AllInboxIcon />, group: DOCUMENTS, gate: { anyOf: SETTINGS_READ },
        render: (ctx) => <Connectors client={ctx.client} canWrite={ctx.permissions.has('settings:write')} /> },
      { id: 'connections', label: 'In use', icon: <SwapHorizIcon />, group: CONNECTIONS, gate: { anyOf: SETTINGS_READ }, refreshContext: true,
        render: (ctx) => whenContextReady(ctx,
          <>
            <Typography variant="h4" component="h1" sx={{ mb: 2 }}>In use</Typography>
            <ProvidersInUse context={ctx.context} canChange={ctx.permissions.has('settings:write')} onNavigate={ctx.navigate} />
            <ProviderAccounts api={rulesApiFor(ctx.client)} canWrite={ctx.permissions.has('settings:write')}
              currency={currency(ctx)} onNavigate={ctx.navigate} />
            {ctx.permissions.has('providers:read') && <DigitalAccounts client={ctx.client}
              canWrite={ctx.permissions.has('providers:write')} />}
            {settingsPage(['providers', 'inbound', 'routes'])(ctx)}
            {ctx.permissions.has('providers:read') && <ReceivingAddresses client={ctx.client} />}
          </>) },
      // Which account sends each fax: the organization's rules, sites, lists and workflows.
      { id: 'rules', label: 'Routing rules', icon: <AltRouteIcon />, group: CONNECTIONS, gate: { anyOf: SETTINGS_READ },
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
        group: CONNECTIONS, gate: { anyOf: SETTINGS_READ }, render: (ctx) => settingsPage(['trunk'], providerLabel('sip'))(ctx) },
      // Choosing or changing providers happens in the Setup wizard.
      { id: 'change', label: 'Add or change a provider', icon: <AddCircleOutlineIcon />, link: 'admin/setup', group: CONNECTIONS,
        gate: { anyOf: ['settings:write'] }, render: () => null },
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
    ],
  },
  {
    id: 'admin', label: 'Administration', icon: <SettingsIcon />,
    pages: [
      { id: 'users', label: 'Users', icon: <PersonIcon />, group: PEOPLE, gate: { anyOf: ['users:read', 'users:manage'] },
        render: (ctx) => <Users client={ctx.client} me={ctx.me} /> },
      { id: 'groups', label: 'Groups', icon: <GroupsIcon />, group: PEOPLE, gate: { anyOf: ['groups:read', 'groups:manage'] },
        render: (ctx) => <Groups client={ctx.client} me={ctx.me} /> },
      { id: 'roles', label: 'Roles', icon: <BadgeIcon />, group: PEOPLE, gate: { anyOf: ['roles:read', 'roles:manage'] },
        render: (ctx) => <Roles client={ctx.client} me={ctx.me} /> },
      { id: 'who', label: 'Who has access', icon: <LockOpenIcon />, group: PEOPLE, gate: { anyOf: ['grants:read', 'grants:manage'] },
        render: (ctx) => <ResourceAccess client={ctx.client} me={ctx.me} section="assignments" /> },
      { id: 'keys', label: 'Keys & phones', icon: <VpnKeyIcon />, group: PEOPLE, gate: { anyOf: ['keys:manage'] },
        render: (ctx) => (
          <>
            <ApiKeys client={ctx.client} me={ctx.me} onlyMine={ctx.params.get('mine') === '1'}
              onShowAll={() => ctx.navigate('admin/keys')} />
            {ctx.permissions.has('settings:read') && <Box sx={{ mt: 4 }}>{settingsPage(['installation-key', 'phones'])(ctx)}</Box>}
          </>
        ) },
      // Everyone can see and end their own sessions.
      { id: 'sessions', label: 'Sessions', icon: <DevicesIcon />, group: PEOPLE, gate: {},
        render: (ctx) => <Sessions client={ctx.client} me={ctx.me} /> },
      { id: 'setup', label: 'Setup', icon: <HelpIcon />, group: INSTALLATION, gate: { anyOf: ['settings:write'] },
        render: (ctx) => <SetupWizard client={ctx.client} onDone={ctx.goHome} docsBase={ctx.docsBase} canRestart={ctx.permissions.has('host:restart')}
          isOwner={isOwner(ctx)} onNavigate={ctx.navigate} /> },
      { id: 'security', label: 'Security', icon: <SecurityIcon />, group: INSTALLATION, gate: { anyOf: SETTINGS_READ },
        render: settingsPage(['security'], 'Security') },
      { id: 'retention', label: 'Documents & retention', icon: <StorageIcon />, group: INSTALLATION, gate: { anyOf: SETTINGS_READ },
        render: settingsPage(['storage', 'advanced', 'backup'], 'Documents & retention') },
      { id: 'analysis', label: 'AI analysis', icon: <SmartToyIcon />, group: INSTALLATION, gate: { anyOf: SETTINGS_READ },
        render: (ctx) => <AIAnalysis client={ctx.client} isOwner={isOwner(ctx)} /> },
      // The organization's NPIs and what the NPI registry lists for them (routing/nppes.py).
      { id: 'npi', label: 'NPI record', icon: <FactCheckIcon />, group: INSTALLATION, gate: { anyOf: SETTINGS_READ },
        render: (ctx) => <NpiRecordPanel client={ctx.client} canWrite={ctx.permissions.has('settings:write')} /> },
      { id: 'health', label: 'System health', icon: <AssessmentIcon />, group: MONITORING, gate: { anyOf: ['diagnostics:read'] },
        refreshContext: true,
        render: (ctx) => whenContextReady(ctx,
          <>
            <Diagnostics client={ctx.client} onNavigate={ctx.navigate} docsBase={ctx.docsBase} />
            {ctx.permissions.has('settings:read') && <Box sx={{ mt: 4 }}>{settingsPage(['diagnostics'])(ctx)}</Box>}
          </>) },
      { id: 'audit', label: 'Audit log', icon: <FactCheckIcon />, group: MONITORING, gate: { anyOf: ['audit:read'] },
        render: (ctx) => (
          <>
            <AuditLog client={ctx.client} canListPeople={ctx.permissions.has('users:read')} />
            {ctx.permissions.has('settings:read') && <Box sx={{ mt: 4 }}>{settingsPage(['audit'])(ctx)}</Box>}
          </>
        ) },
      { id: 'logs', label: 'Logs', icon: <DescriptionIcon />, group: MONITORING, gate: { anyOf: ['logs:read'] },
        render: (ctx) => <Logs client={ctx.client} /> },
      { id: 'api', label: 'API & SDKs', icon: <ApiIcon />, gate: OVERVIEW_GATE, group: DEVELOPER,
        render: (ctx) => (
          <>
            <DeveloperOverview client={ctx.client} />
            {ctx.permissions.has('settings:read') && <Box sx={{ mt: 4 }}>{settingsPage(['developer'])(ctx)}</Box>}
          </>
        ) },
      { id: 'assistants', label: 'AI assistants', icon: <SmartToyIcon />, gate: { anyOf: SETTINGS_READ }, group: DEVELOPER,
        render: (ctx) => (
          <>
            <AssistantsOverview client={ctx.client} />
            <MCP client={ctx.client} />
            {settingsPage(['mcp'])(ctx)}
          </>
        ) },
      { id: 'terminal', label: 'Terminal', icon: <TerminalIcon />, gate: { anyOf: ['host:terminal'] }, group: DEVELOPER,
        render: (ctx) => (
          <>
            <Terminal client={ctx.client} />
            <DeploymentSection client={ctx.client} names={['ENABLE_ADMIN_EXEC']} title="Terminal setting" showNames />
          </>
        ) },
      { id: 'scripts', label: 'Scripts & checks', icon: <ScienceIcon />, gate: { anyOf: ['providers:write'] }, group: DEVELOPER,
        refreshContext: true,
        render: (ctx) => whenContextReady(ctx, <ScriptsTests client={ctx.client} onNavigate={ctx.navigate} docsBase={ctx.docsBase} />) },
      // Always listed, so plugins can be turned on here; the installed plugins show once they are on.
      { id: 'plugins', label: 'Provider plugins', icon: <ExtensionIcon />, gate: { anyOf: ['providers:read', 'settings:read'] },
        group: DEVELOPER, refreshContext: true,
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
    inPanel: page.inPanel !== false && (!page.provider || inUse === null || inUse.includes(page.provider)),
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
  kind: 'page';
  area: NavArea;
  page: NavPage;
  // The address this page is shown at; differs from the requested one when only the
  // area was named or the address moved.
  address: string;
  // Where the requested old address used to be in the menu ('Costs › Savings').
  movedFrom?: string;
}

// An address that names no page ('unknown'), or a page this person may not open
// ('forbidden'). The address stays as it was asked for, and no page is shown.
export interface AddressProblem {
  kind: 'unknown' | 'forbidden';
  address: string;
}

export type Resolution = ResolvedPage | AddressProblem;

function has<T extends object>(table: T, key: string): key is Extract<keyof T, string> {
  return Object.prototype.hasOwnProperty.call(table, key);
}

// The new home of an old address. Its fixed query (show=waiting) comes first; the old
// address keeps its own query after it.
function follow(requested: ParsedAddress): { target: ParsedAddress; was: string } | null {
  const key = `${requested.area}/${requested.page}`;
  if (!requested.page || !has(MOVED_ADDRESSES, key)) return null;
  const target = parseAddress(`#/${MOVED_ADDRESSES[key]}`);
  if (!target) return null;
  const fixed = new Set(target.params.keys());
  requested.params.forEach((value, name) => { if (!fixed.has(name)) target.params.append(name, value); });
  return { target, was: MOVED_FROM[key] };
}

// The page for an address among the visible ones. Only the area named: its first page
// this person may open. An old address: the page that does that job now, with its query.
// No address at all: the first page this person may open. An address that names no page,
// or a page this person may not open, is reported as such and never shows a page.
export function resolveAddress(visible: NavArea[], requested: ParsedAddress | null): Resolution | null {
  if (visible.length === 0) return null;
  const first = visible[0];
  if (!requested) return { kind: 'page', area: first, page: first.pages[0], address: pageAddress(first.id, first.pages[0].id) };
  const asked = pageAddress(requested.area, requested.page ?? undefined, requested.params);
  const forbidden: AddressProblem = { kind: 'forbidden', address: asked };
  const unknown: AddressProblem = { kind: 'unknown', address: asked };

  // A former area on its own (#/costs) opens the first of its old pages this person may open.
  if (!requested.page && has(FORMER_AREAS, requested.area)) {
    for (const [from] of MOVES.filter(([old]) => old.startsWith(`${requested.area}/`))) {
      const [area, page] = from.split('/');
      const found = resolveAddress(visible, { area, page, params: new URLSearchParams() });
      if (found?.kind === 'page') return { ...found, movedFrom: FORMER_AREAS[requested.area] };
    }
    return forbidden;
  }

  const moved = follow(requested);
  const parsed = moved?.target ?? requested;
  const known = NAVIGATION.find((area) => area.id === parsed.area);
  if (!known) return unknown;
  const single = singlePage(known.id);
  const pageId = parsed.page ?? (single ? known.pages[0].id : null);
  if (pageId && !known.pages.some((page) => page.id === pageId)) return unknown;
  const area = visible.find((candidate) => candidate.id === known.id);
  const page = area && (pageId ? area.pages.find((candidate) => candidate.id === pageId) : area.pages[0]);
  if (!area || !page) return forbidden;
  // An entry that opens another page, such as Add or change a provider.
  if (page.link) {
    const target = resolveAddress(visible, parseAddress(destinationAddress(page.link)));
    return target?.kind === 'page' && moved ? { ...target, movedFrom: moved.was } : target;
  }
  // Only the area named: its first page, without the query (one-page areas keep it).
  const keep = pageId || single ? parsed.params : undefined;
  return { kind: 'page', area, page, address: pageAddress(area.id, page.id, keep), movedFrom: moved?.was };
}

// The address part of a destination: a legacy name through LEGACY_DESTINATIONS, an
// old address through MOVED_ADDRESSES (keeping its query), or 'area/page' (with an
// optional ?query) as given. The new address is written directly, so a link inside
// the console never shows the moved notice.
export function destinationAddress(destination: AdminDestination): string {
  const path: string = has(LEGACY_DESTINATIONS, destination) ? LEGACY_DESTINATIONS[destination] : destination;
  const parsed = parseAddress(`#/${path}`);
  if (!parsed) {
    const [route, query] = path.split('?');
    const [area, page] = route.split('/');
    return pageAddress(area, page, query);
  }
  const target = follow(parsed)?.target ?? parsed;
  return pageAddress(target.area, target.page ?? undefined, target.params);
}

// Where a destination leads for someone who may see every page: the area and page an
// address, an old address or a legacy name opens, or null when it names no page. For
// checks such as "every capability's setting address opens a page".
export function resolvesTo(destination: string): { area: NavArea; page: NavPage } | null {
  const found = resolveAddress(NAVIGATION, parseAddress(destinationAddress(destination as AdminDestination)));
  return found?.kind === 'page' ? { area: found.area, page: found.page } : null;
}
