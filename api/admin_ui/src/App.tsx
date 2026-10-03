import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Box,
  AppBar,
  Toolbar,
  Typography,
  Container,
  Alert,
  Button,
  Paper,
  Tabs,
  Tab,
  IconButton,
  Drawer,
  List,
  ListItemButton,
  ListItemText,
  ListItemIcon,
  Divider,
  Tooltip,
  useMediaQuery,
  useTheme as useMuiTheme,
  Fade,
  CircularProgress,
} from '@mui/material';
import MenuIcon from '@mui/icons-material/Menu';
import SettingsIcon from '@mui/icons-material/Settings';
import DashboardIcon from '@mui/icons-material/Dashboard';
import SendIcon from '@mui/icons-material/Send';
import ListAltIcon from '@mui/icons-material/ListAlt';
import InboxIcon from '@mui/icons-material/Inbox';
import VpnKeyIcon from '@mui/icons-material/VpnKey';
import CodeIcon from '@mui/icons-material/Code';
import TerminalIcon from '@mui/icons-material/Terminal';
import AssessmentIcon from '@mui/icons-material/Assessment';
import DescriptionIcon from '@mui/icons-material/Description';
import ExtensionIcon from '@mui/icons-material/Extension';
import ScienceIcon from '@mui/icons-material/Science';
import LogoutIcon from '@mui/icons-material/Logout';
import HelpIcon from '@mui/icons-material/Help';
import PersonIcon from '@mui/icons-material/Person';
import GroupsIcon from '@mui/icons-material/Groups';
import BadgeIcon from '@mui/icons-material/Badge';
import LockOpenIcon from '@mui/icons-material/LockOpen';
import DevicesIcon from '@mui/icons-material/Devices';
import AltRouteIcon from '@mui/icons-material/AltRoute';
import MoveToInboxIcon from '@mui/icons-material/MoveToInbox';
import AdminAPIClient, { AdminAPIError, type ClientCredential } from './api/client';
import Dashboard from './components/Dashboard';
import SetupWizard from './components/SetupWizard';
import JobsList from './components/JobsList';
import Plugins from './components/Plugins';
import ApiKeys from './components/ApiKeys';
import Settings from './components/Settings';
import Diagnostics from './components/Diagnostics';
import MCP from './components/MCP';
import Logs from './components/Logs';
import SendFax from './components/SendFax';
import Inbound from './components/Inbound';
import Terminal from './components/Terminal';
import ScriptsTests from './components/ScriptsTests';
import Users from './components/Users';
import Groups from './components/Groups';
import Roles from './components/Roles';
import ResourceAccess from './components/ResourceAccess';
import Sessions from './components/Sessions';
import DeliveryRoutes from './components/DeliveryRoutes';
import Intake from './components/Intake';
import LoginScreen from './components/LoginScreen';
import PasswordChange from './components/PasswordChange';
import OwnerEnrollment from './components/OwnerEnrollment';
import { ThemeProvider } from './theme/ThemeContext';
import { ThemeToggle } from './components/ThemeToggle';
import {
  visibleSettings,
  visibleTools,
  visibleTopTabs,
  type AdminDestination,
  type SettingsTab,
  type ToolTab,
  type TopTab,
} from './navigation';
import type { AdminConfig, AuthMe, ConsoleContext } from './api/types';

// The console once kept an API key here. It is removed on first boot and
// never replayed: secrets do not live in persistent browser storage.
const LEGACY_KEY_STORAGE = 'faxbot_admin_key';
const SESSION_ENDED = 'Your session has ended. Sign in again.';

function signInError(error: unknown, kind: 'password' | 'key'): string {
  if (error instanceof AdminAPIError) {
    if (error.status === 403 && kind === 'password') {
      return 'Password sign-in needs a secure (HTTPS) connection here. Sign in with an API key instead.';
    }
    if ([401, 429, 503].includes(error.status) && error.detail) return error.detail;
    if (error.status === 400 || error.status === 422) {
      return kind === 'password' ? 'Enter your username and password.' : 'Enter a valid API key.';
    }
    if (error.status === 401) return kind === 'password' ? 'The username or password is not correct.' : 'The API key is not valid.';
    return 'Sign-in failed. Try again.';
  }
  if (error instanceof TypeError) return 'Could not reach the server. Check the connection and try again.';
  return 'Sign-in failed. Try again.';
}

function bootNotice(error: unknown): string | undefined {
  if (error instanceof TypeError) return 'Could not reach the server. Check the connection and try again.';
  if (error instanceof AdminAPIError && error.status === 503) return 'The server is busy. Try again in a moment.';
  return undefined;
}

type AuthState =
  | { phase: 'booting' }
  | { phase: 'signed-out'; notice?: string }
  | { phase: 'change-password'; client: AdminAPIClient; me: AuthMe }
  | { phase: 'ready'; client: AdminAPIClient; me: AuthMe; context: ConsoleContext };

function AppContent() {
  const [auth, setAuth] = useState<AuthState>({ phase: 'booting' });
  const activeClient = useRef<AdminAPIClient | null>(null);

  const makeClient = useCallback((credential: ClientCredential) => {
    const client: AdminAPIClient = new AdminAPIClient(credential, {
      onUnauthorized: () => {
        if (activeClient.current !== client) return;
        activeClient.current = null;
        setAuth({ phase: 'signed-out', notice: SESSION_ENDED });
      },
    });
    return client;
  }, []);

  const establish = useCallback(async (client: AdminAPIClient) => {
    const me = await client.me({ quiet401: true });
    activeClient.current = client;
    if (me.password_change_required) {
      setAuth({ phase: 'change-password', client, me });
      return;
    }
    const context = await client.context();
    setAuth({ phase: 'ready', client, me, context });
  }, []);

  useEffect(() => {
    try { window.localStorage.removeItem(LEGACY_KEY_STORAGE); } catch { /* storage unavailable */ }
    let cancelled = false;
    establish(makeClient({ kind: 'session', csrf: null })).catch((error) => {
      if (!cancelled) setAuth({ phase: 'signed-out', notice: bootNotice(error) });
    });
    return () => { cancelled = true; };
  }, [establish, makeClient]);

  const passwordSignIn = useCallback(async (login: string, password: string) => {
    try {
      await AdminAPIClient.login(login, password);
      await establish(makeClient({ kind: 'session', csrf: null }));
    } catch (error) {
      throw new Error(signInError(error, 'password'));
    }
  }, [establish, makeClient]);

  const keySignIn = useCallback(async (apiKey: string) => {
    try {
      await AdminAPIClient.keyLogin(apiKey);
    } catch (error) {
      if (!(error instanceof AdminAPIError && error.status === 403)) throw new Error(signInError(error, 'key'));
      // Browser sessions are not allowed on this connection. Use the key
      // directly for this tab only; it is kept in memory and never stored.
      try {
        await establish(makeClient({ kind: 'key', key: apiKey }));
      } catch (inner) {
        throw new Error(signInError(inner, 'key'));
      }
      return;
    }
    try {
      await establish(makeClient({ kind: 'session', csrf: null }));
    } catch (error) {
      throw new Error(signInError(error, 'key'));
    }
  }, [establish, makeClient]);

  const signOut = useCallback(async () => {
    const client = activeClient.current;
    activeClient.current = null;
    try { await client?.logout(); } catch { /* the local session ends either way */ }
    setAuth({ phase: 'signed-out' });
  }, []);

  const refreshIdentity = useCallback(async () => {
    if (auth.phase !== 'ready' && auth.phase !== 'change-password') return;
    await establish(auth.client);
  }, [auth, establish]);

  if (auth.phase === 'booting') {
    return (
      <Box role="status" aria-live="polite" sx={{
        minHeight: '100vh', display: 'flex', flexDirection: 'column',
        alignItems: 'center', justifyContent: 'center', gap: 2,
      }}>
        <CircularProgress aria-label="Loading" />
        <Typography variant="body1">Loading…</Typography>
      </Box>
    );
  }

  if (auth.phase === 'signed-out') {
    return <LoginScreen notice={auth.notice} onPasswordSignIn={passwordSignIn} onKeySignIn={keySignIn} />;
  }

  if (auth.phase === 'change-password') {
    return (
      <PasswordChange
        client={auth.client}
        displayName={auth.me.principal.display_name}
        onChanged={refreshIdentity}
        onSignOut={() => void signOut()}
      />
    );
  }

  return (
    <ConsoleShell
      client={auth.client}
      me={auth.me}
      initialContext={auth.context}
      onSignOut={() => void signOut()}
      onIdentityChanged={() => { void refreshIdentity().catch(() => undefined); }}
    />
  );
}

interface TabPanelProps {
  children?: React.ReactNode;
  value: string;
  current: string;
}

function TabPanel({ children, value, current }: TabPanelProps) {
  return (
    <Fade in={value === current} timeout={300}>
      <div role="tabpanel" hidden={value !== current} id={`admin-tabpanel-${value}`}>
        {value === current && <Box sx={{ py: { xs: 2, md: 3 } }}>{children}</Box>}
      </div>
    </Fade>
  );
}

const TOP_LABELS: Record<TopTab, string> = {
  dashboard: 'Dashboard', send: 'Send', jobs: 'Jobs', inbox: 'Inbox', settings: 'Settings', tools: 'Tools',
};

const TOP_ICONS: Record<TopTab, React.ReactElement> = {
  dashboard: <DashboardIcon />, send: <SendIcon />, jobs: <ListAltIcon />, inbox: <InboxIcon />,
  settings: <SettingsIcon />, tools: <ScienceIcon />,
};

const SETTINGS_ICONS: Record<SettingsTab, React.ReactElement> = {
  setup: <HelpIcon />, settings: <SettingsIcon />, keys: <VpnKeyIcon />, users: <PersonIcon />,
  groups: <GroupsIcon />, roles: <BadgeIcon />, access: <LockOpenIcon />, sessions: <DevicesIcon />, mcp: <CodeIcon />,
};

const TOOL_ICONS: Record<ToolTab, React.ReactElement> = {
  routes: <AltRouteIcon />, intake: <MoveToInboxIcon />,
  terminal: <TerminalIcon />, diagnostics: <AssessmentIcon />, logs: <DescriptionIcon />,
  plugins: <ExtensionIcon />, scripts: <ScienceIcon />,
};

interface ConsoleShellProps {
  client: AdminAPIClient;
  me: AuthMe;
  initialContext: ConsoleContext;
  onSignOut: () => void;
  onIdentityChanged: () => void;
}

function ConsoleShell({ client, me, initialContext, onSignOut, onIdentityChanged }: ConsoleShellProps) {
  const muiTheme = useMuiTheme();
  const isMobile = useMediaQuery(muiTheme.breakpoints.down('md'));
  const isTablet = useMediaQuery(muiTheme.breakpoints.down('lg'));

  const [context, setContext] = useState<ConsoleContext>(initialContext);
  const [contextLoading, setContextLoading] = useState(false);
  const [contextError, setContextError] = useState<string | null>(null);
  const [mobileOpen, setMobileOpen] = useState(false);

  const permissions = useMemo(() => new Set(me.permissions), [me.permissions]);
  const pluginsEnabled = Boolean(context.provider_view?.plugins_enabled);
  const settingsItems = useMemo(() => visibleSettings(permissions), [permissions]);
  const toolsItems = useMemo(() => visibleTools(permissions, pluginsEnabled), [permissions, pluginsEnabled]);
  const topTabs = useMemo(
    () => visibleTopTabs(permissions, context.navigation, toolsItems.length > 0),
    [permissions, context.navigation, toolsItems.length],
  );

  const [tab, setTab] = useState<TopTab>(() => topTabs[0]);
  const [settingsTab, setSettingsTab] = useState<SettingsTab>(() => settingsItems[0]?.value ?? 'sessions');
  const [toolsTab, setToolsTab] = useState<ToolTab | null>(() => toolsItems[0]?.value ?? null);

  const currentTab = topTabs.includes(tab) ? tab : topTabs[0];
  const currentSettings = settingsItems.some((item) => item.value === settingsTab) ? settingsTab : settingsItems[0].value;
  const currentTool = toolsItems.find((item) => item.value === toolsTab)?.value ?? toolsItems[0]?.value ?? null;

  const docsBase = context.branding?.docs_base;
  const adminConfig: AdminConfig | null = context.send ? {
    fax_disabled: context.send.fax_disabled,
    max_file_size_mb: context.send.max_file_size_mb,
    branding: context.branding,
    inbound: { enabled: Boolean(context.inbound_enabled) },
    v3_plugins: { enabled: pluginsEnabled },
  } : null;

  const changeTab = (next: TopTab) => {
    if ((next === 'send' || next === 'inbox' || next === 'tools') && currentTab !== next) {
      setContextLoading(true);
      setContextError(null);
    }
    setTab(next);
    if (isMobile) setMobileOpen(false);
  };

  const openSettings = (next: SettingsTab) => {
    setSettingsTab(next);
    changeTab('settings');
  };

  const handleNavigate = (destination: AdminDestination) => {
    switch (destination) {
      case 'send':
      case 'jobs':
      case 'inbox':
        if (topTabs.includes(destination)) changeTab(destination);
        break;
      case 'settings':
        openSettings('settings');
        break;
      case 'keys':
        openSettings('keys');
        break;
      case 'diagnostics':
        if (toolsItems.some((item) => item.value === 'diagnostics')) {
          setToolsTab('diagnostics');
          changeTab('tools');
        }
        break;
    }
  };

  // Sections that depend on active settings refresh the console context on entry.
  useEffect(() => {
    if (currentTab !== 'send' && currentTab !== 'inbox' && currentTab !== 'tools') return;
    let current = true;
    setContextLoading(true);
    setContextError(null);
    client.context().then((next) => {
      if (current) setContext(next);
    }).catch(() => {
      if (current) setContextError('Could not load current settings. Leave and reopen this section to try again.');
    }).finally(() => {
      if (current) setContextLoading(false);
    });
    return () => { current = false; };
  }, [client, currentTab]);

  const tabsSx = {
    '& .MuiTab-root': {
      minWidth: { xs: 'auto', sm: 90 },
      fontSize: { xs: '0.75rem', sm: '0.875rem' },
      px: { xs: 1, sm: 2 },
      transition: 'all 0.2s',
      borderRadius: '8px 8px 0 0',
      '&:hover': { backgroundColor: muiTheme.palette.action.hover },
    },
    '& .MuiTabs-indicator': { height: 3, borderRadius: '3px 3px 0 0' },
  };

  const groupPaperSx = { borderRadius: 3, overflow: 'hidden', border: '1px solid', borderColor: 'divider' };
  const groupHeaderSx = { borderBottom: 1, borderColor: 'divider', backgroundColor: muiTheme.palette.action.hover };

  const drawerItemSx = { borderRadius: '0 24px 24px 0', mx: 1, mb: 0.5 };

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', minHeight: '100vh' }}>
      <AppBar
        position="sticky"
        elevation={0}
        sx={{
          color: 'text.primary',
          backdropFilter: 'blur(10px)',
          background: muiTheme.palette.mode === 'dark' ? 'rgba(15, 15, 17, 0.9)' : 'rgba(255, 255, 255, 0.9)',
          borderBottom: '1px solid',
          borderColor: 'divider',
        }}
      >
        <Toolbar sx={{ minHeight: { xs: 56, sm: 64 }, px: { xs: 1, sm: 2 } }}>
          <IconButton
            color="inherit"
            edge="start"
            onClick={() => setMobileOpen(true)}
            sx={{ mr: 1, display: { xs: 'inline-flex', md: 'none' } }}
            aria-label="open navigation"
          >
            <MenuIcon />
          </IconButton>
          <Box
            component="img"
            src={muiTheme.palette.mode === 'dark' ? '/admin/ui/faxbot_mini_banner_dark.png' : '/admin/ui/faxbot_mini_banner_light.png'}
            alt="Faxbot"
            onClick={() => changeTab(topTabs[0])}
            sx={{
              height: { xs: 30, sm: 36 },
              mr: { xs: 1, sm: 2 },
              borderRadius: 1,
              cursor: 'pointer',
              transition: 'all 0.3s cubic-bezier(0.4, 0, 0.2, 1)',
              '&:hover': { opacity: 0.8, transform: 'scale(1.05)' },
            }}
          />
          <Typography
            variant="h6"
            sx={{ flexGrow: 1, fontSize: { xs: '0.95rem', sm: '1.2rem' }, fontWeight: 600, display: { xs: 'none', md: 'block' } }}
          >
            Admin Console
          </Typography>
          <Box sx={{ flexGrow: { xs: 1, md: 0 } }} />
          <Typography variant="body2" color="text.secondary" sx={{ mr: 1, display: { xs: 'none', sm: 'block' } }}>
            {me.principal.display_name}
          </Typography>
          <ThemeToggle />
          <Tooltip title="Open Settings">
            <IconButton
              color="inherit"
              onClick={() => openSettings(settingsItems.some((item) => item.value === 'settings') ? 'settings' : settingsItems[0].value)}
              sx={{ mx: 1 }}
              aria-label="open settings"
            >
              <SettingsIcon />
            </IconButton>
          </Tooltip>
          <Button
            color="inherit"
            onClick={onSignOut}
            startIcon={<LogoutIcon />}
            size={isMobile ? 'small' : 'medium'}
            sx={{ fontSize: { xs: '0.75rem', sm: '0.875rem' }, borderRadius: 2, px: { xs: 1.5, sm: 2 } }}
          >
            Sign out
          </Button>
        </Toolbar>
      </AppBar>

      <Container maxWidth="xl" sx={{ flex: 1, px: { xs: 1, sm: 2, md: 3 } }}>
        {me.can_enroll_owner && (
          <Box sx={{ mt: 2 }}>
            <OwnerEnrollment client={client} onEnrolled={onIdentityChanged} />
          </Box>
        )}

        {!isMobile && (
          <Box sx={{ borderBottom: 1, borderColor: 'divider', mt: { xs: 1, md: 2 } }}>
            <Tabs
              value={currentTab}
              onChange={(_, next: TopTab) => changeTab(next)}
              variant={isTablet ? 'scrollable' : 'standard'}
              scrollButtons={isTablet ? 'auto' : false}
              allowScrollButtonsMobile
              sx={tabsSx}
            >
              {topTabs.map((value) => (
                <Tab key={value} value={value} icon={TOP_ICONS[value]} iconPosition="start" label={TOP_LABELS[value]} />
              ))}
            </Tabs>
          </Box>
        )}

        <Drawer
          anchor="left"
          open={mobileOpen}
          onClose={() => setMobileOpen(false)}
          sx={{ display: { xs: 'block', md: 'none' }, '& .MuiDrawer-paper': { width: 280, borderRadius: '0 16px 16px 0' } }}
        >
          <Box sx={{ width: 280, pt: 2 }} role="presentation" onClick={() => setMobileOpen(false)}>
            <Box sx={{ px: 2, pb: 2 }}>
              <Typography variant="h6" fontWeight={600}>Navigation</Typography>
            </Box>
            <List>
              {topTabs.filter((value) => value !== 'settings' && value !== 'tools').map((value) => (
                <ListItemButton key={value} selected={currentTab === value} onClick={() => changeTab(value)} sx={drawerItemSx}>
                  <ListItemIcon>{TOP_ICONS[value]}</ListItemIcon>
                  <ListItemText primary={TOP_LABELS[value]} />
                </ListItemButton>
              ))}
              <Divider sx={{ my: 2 }} />
              <ListItemText primary="Settings" primaryTypographyProps={{ fontWeight: 600 }} sx={{ px: 3 }} />
              {settingsItems.map((item) => (
                <ListItemButton key={item.value} selected={currentTab === 'settings' && currentSettings === item.value}
                  onClick={() => openSettings(item.value)} sx={{ ...drawerItemSx, pl: 4 }}>
                  <ListItemIcon>{SETTINGS_ICONS[item.value]}</ListItemIcon>
                  <ListItemText primary={item.label} />
                </ListItemButton>
              ))}
              {toolsItems.length > 0 && (
                <>
                  <Divider sx={{ my: 2 }} />
                  <ListItemText primary="Tools" primaryTypographyProps={{ fontWeight: 600 }} sx={{ px: 3 }} />
                  {toolsItems.map((item) => (
                    <ListItemButton key={item.value} selected={currentTab === 'tools' && currentTool === item.value}
                      onClick={() => { setToolsTab(item.value); changeTab('tools'); }} sx={{ ...drawerItemSx, pl: 4 }}>
                      <ListItemIcon>{TOOL_ICONS[item.value]}</ListItemIcon>
                      <ListItemText primary={item.label} />
                    </ListItemButton>
                  ))}
                </>
              )}
            </List>
          </Box>
        </Drawer>

        <TabPanel value="dashboard" current={currentTab}>
          <Dashboard client={client} onNavigate={handleNavigate} />
        </TabPanel>
        <TabPanel value="send" current={currentTab}>
          <SendFax client={client} config={adminConfig} configLoading={contextLoading} configError={contextError} />
        </TabPanel>
        <TabPanel value="jobs" current={currentTab}>
          <JobsList client={client} />
        </TabPanel>
        <TabPanel value="inbox" current={currentTab}>
          {contextLoading ? <Alert severity="info">Loading…</Alert>
            : contextError ? <Alert severity="error">{contextError}</Alert>
            : <Inbound client={client} inboundEnabled={context.inbound_enabled ?? undefined} onNavigate={handleNavigate} docsBase={docsBase} permissions={permissions} />}
        </TabPanel>
        <TabPanel value="settings" current={currentTab}>
          <Paper elevation={0} sx={groupPaperSx}>
            <Box sx={groupHeaderSx}>
              <Tabs
                value={currentSettings}
                onChange={(_, next: SettingsTab) => setSettingsTab(next)}
                variant="scrollable"
                scrollButtons="auto"
                sx={{ px: 2 }}
              >
                {settingsItems.map((item) => (
                  <Tab key={item.value} value={item.value} icon={SETTINGS_ICONS[item.value]} iconPosition="start" label={item.label} />
                ))}
              </Tabs>
            </Box>
            <Box sx={{ p: { xs: 2, md: 3 } }}>
              {currentSettings === 'setup' && <SetupWizard client={client} onDone={() => changeTab(topTabs[0])} docsBase={docsBase} />}
              {currentSettings === 'settings' && <Settings client={client} />}
              {currentSettings === 'keys' && <ApiKeys client={client} me={me} />}
              {currentSettings === 'users' && <Users client={client} me={me} />}
              {currentSettings === 'groups' && <Groups client={client} me={me} />}
              {currentSettings === 'roles' && <Roles client={client} me={me} />}
              {currentSettings === 'access' && <ResourceAccess client={client} me={me} />}
              {currentSettings === 'sessions' && <Sessions client={client} me={me} />}
              {currentSettings === 'mcp' && <MCP client={client} />}
            </Box>
          </Paper>
        </TabPanel>
        <TabPanel value="tools" current={currentTab}>
          {contextLoading ? <Alert severity="info">Loading…</Alert>
            : contextError ? <Alert severity="error">{contextError}</Alert>
            : currentTool && (
              <Paper elevation={0} sx={groupPaperSx}>
                <Box sx={groupHeaderSx}>
                  <Tabs
                    value={currentTool}
                    onChange={(_, next: ToolTab) => setToolsTab(next)}
                    variant={isMobile ? 'scrollable' : 'standard'}
                    scrollButtons={isMobile ? 'auto' : false}
                    sx={{ px: 2 }}
                  >
                    {toolsItems.map((item) => (
                      <Tab key={item.value} value={item.value} icon={TOOL_ICONS[item.value]} iconPosition="start" label={item.label} />
                    ))}
                  </Tabs>
                </Box>
                <Box sx={{ p: { xs: 2, md: 3 } }}>
                  {currentTool === 'routes' && <DeliveryRoutes client={client} canWrite={permissions.has('settings:write')} />}
                  {currentTool === 'intake' && <Intake client={client} canWrite={permissions.has('settings:write')} />}
                  {currentTool === 'terminal' && <Terminal client={client} />}
                  {currentTool === 'diagnostics' && <Diagnostics client={client} onNavigate={handleNavigate} docsBase={docsBase} />}
                  {currentTool === 'logs' && <Logs client={client} />}
                  {currentTool === 'plugins' && <Plugins client={client} config={adminConfig} configLoading={contextLoading} configError={contextError} onNavigate={handleNavigate} />}
                  {currentTool === 'scripts' && <ScriptsTests client={client} onNavigate={handleNavigate} docsBase={docsBase} />}
                </Box>
              </Paper>
            )}
        </TabPanel>
      </Container>
    </Box>
  );
}

function App() {
  return (
    <ThemeProvider>
      <AppContent />
    </ThemeProvider>
  );
}

export default App;
