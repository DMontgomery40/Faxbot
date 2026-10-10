import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Box,
  AppBar,
  Toolbar,
  Typography,
  Alert,
  IconButton,
  Drawer,
  useMediaQuery,
  useTheme as useMuiTheme,
  CircularProgress,
} from '@mui/material';
import MenuIcon from '@mui/icons-material/Menu';
import AdminAPIClient, { AdminAPIError, type ClientCredential } from './api/client';
import LoginScreen from './components/LoginScreen';
import PasswordChange from './components/PasswordChange';
import OwnerEnrollment from './components/OwnerEnrollment';
import NavPanel, { SendFaxButton, type SendAction } from './components/shell/NavPanel';
import PageBreadcrumbs from './components/shell/PageBreadcrumbs';
import { AddressProblem, MovedNotice } from './components/shell/AddressState';
import UserMenu from './components/shell/UserMenu';
import { ThemeProvider } from './theme/ThemeContext';
import { setProviderNames } from './providerLabels';
import {
  destinationAddress,
  pageAddress,
  parseAddress,
  resolveHash,
  providersInUse,
  visibleNavigation,
  type AdminDestination,
  type NavArea,
  type NavPage,
  type PageContext,
} from './navigation';
import type { AdminConfig, AuthMe, ConsoleContext } from './api/types';
import { contextNumberFormat } from './components/common/numbers';

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


// Wide enough for the longest area name (Savings & optimization) on one line.
const DRAWER_WIDTH = 288;
const CONTEXT_FAILED = 'Could not load current settings. Leave and reopen this section to try again.';

interface ConsoleShellProps {
  client: AdminAPIClient;
  me: AuthMe;
  initialContext: ConsoleContext;
  onSignOut: () => void;
  onIdentityChanged: () => void;
}

// The address in the browser bar is the page shown; Back and Forward change it.
function useAddress(): [string, (next: string) => void] {
  const [hash, setHash] = useState(() => window.location.hash);
  useEffect(() => {
    const follow = () => setHash(window.location.hash);
    window.addEventListener('hashchange', follow);
    return () => window.removeEventListener('hashchange', follow);
  }, []);
  const go = useCallback((next: string) => {
    if (window.location.hash !== next) window.location.hash = next;
    setHash(next);
  }, []);
  return [hash, go];
}

// Settings the console context reflects (provider names, the panel, Send's limits).
const CONTEXT_SETTINGS = new Set([
  'backend', 'outbound_backend', 'inbound_backend', 'inbound_enabled', 'outbound_routes', 'sip_trunk_preset',
  'feature_v3_plugins', 'feature_plugin_install', 'fax_disabled', 'max_file_size_mb', 'fax_default_country', 'docs_base_url',
]);

function ConsoleShell({ client, me, initialContext, onSignOut, onIdentityChanged }: ConsoleShellProps) {
  const muiTheme = useMuiTheme();
  const isMobile = useMediaQuery(muiTheme.breakpoints.down('md'));

  const [context, setContext] = useState<ConsoleContext>(initialContext);
  // Every screen names the trunk by its carrier, from the context in hand.
  setProviderNames(context.provider_names);
  const [mobileOpen, setMobileOpen] = useState(false);
  // A fax Send just queued, opened in Sent when the person follows it.
  const [jobToOpen, setJobToOpen] = useState<string | null>(null);

  const permissions = useMemo(() => new Set(me.permissions), [me.permissions]);
  const pluginsEnabled = Boolean(context.provider_view?.plugins_enabled);
  const visible = useMemo(
    () => visibleNavigation(permissions, context.navigation, { pluginsEnabled, providersInUse: providersInUse(context) }),
    [permissions, context, pluginsEnabled],
  );

  const [hash, setAddress] = useAddress();
  const route = resolveHash(visible, hash);
  // The page shown; none while the address names no page this person may open.
  const shown = route?.kind === 'page' ? route : null;
  const pageKey = shown ? `${shown.area.id}/${shown.page.id}` : '';
  // An old address that moved: the notice above the page it opens now.
  const [moved, setMoved] = useState<{ address: string; was: string } | null>(null);

  // No address, an area on its own, or an old address shows the page that does the job;
  // the address is corrected in place, without a history entry. An unknown or forbidden
  // address stays as it is, with its own short state instead of a page.
  const correctTo = shown && shown.address !== hash ? shown.address : null;
  const movedFrom = shown?.movedFrom;
  useEffect(() => {
    if (!correctTo) return;
    if (movedFrom) setMoved({ address: correctTo, was: movedFrom });
    window.history.replaceState(window.history.state, '', correctTo);
    setAddress(correctTo);
  }, [correctTo, movedFrom, setAddress]);

  const navigateTo = useCallback((address: string) => {
    setMoved(null);
    setAddress(address);
    setMobileOpen(false);
    window.scrollTo?.({ top: 0 });
  }, [setAddress]);
  const openPage = useCallback((area: NavArea, page: NavPage) => navigateTo(pageAddress(area.id, page.id)), [navigateTo]);
  const navigate = useCallback((destination: AdminDestination) => navigateTo(destinationAddress(destination)), [navigateTo]);
  const goHome = useCallback(() => {
    if (visible[0]) openPage(visible[0], visible[0].pages[0]);
  }, [visible, openPage]);

  // A saved change to providers, routes or the trunk's carrier (or a restart) changes what the
  // panel and every page name: read the console context again at once.
  useEffect(() => client.onSettingsChanged((changed) => {
    if (changed.length > 0 && !changed.some((name) => CONTEXT_SETTINGS.has(name))) return;
    client.context().then(setContext).catch(() => undefined);
  }), [client]);

  // Pages that show active settings read the console context again on each entry.
  const [refresh, setRefresh] = useState<{ key: string; state: 'loading' | 'ready' | 'error' }>({ key: '', state: 'ready' });
  const refreshes = Boolean(shown?.page.refreshContext);
  useEffect(() => {
    if (!refreshes) return;
    let current = true;
    setRefresh({ key: pageKey, state: 'loading' });
    client.context().then((next) => {
      if (!current) return;
      setContext(next);
      setRefresh({ key: pageKey, state: 'ready' });
    }).catch(() => {
      if (current) setRefresh({ key: pageKey, state: 'error' });
    });
    return () => { current = false; };
  }, [client, pageKey, refreshes]);
  const contextLoading = refreshes && (refresh.key !== pageKey || refresh.state === 'loading');
  const contextError = refreshes && refresh.key === pageKey && refresh.state === 'error' ? CONTEXT_FAILED : null;

  const docsBase = context.branding?.docs_base;
  // The Setup Wizard is for people who may change settings.
  const canSetUp = permissions.has('settings:write');
  const adminConfig: AdminConfig | null = context.send ? {
    fax_disabled: context.send.fax_disabled,
    max_file_size_mb: context.send.max_file_size_mb,
    number_format: contextNumberFormat(context.send),
    branding: context.branding,
    inbound: { enabled: Boolean(context.inbound_enabled) },
    v3_plugins: { enabled: pluginsEnabled },
  } : null;

  const pageContext: PageContext = {
    client, me, permissions, context, adminConfig, contextLoading, contextError, docsBase, canSetUp,
    params: shown ? parseAddress(shown.address)?.params ?? new URLSearchParams() : new URLSearchParams(),
    navigate, goHome, jobToOpen,
    openJob: (jobId) => { setJobToOpen(jobId); navigate('jobs'); },
    jobOpened: () => setJobToOpen(null),
  };


  const first = visible[0];
  const homeAddress = first ? pageAddress(first.id, first.pages[0].id) : '#';
  const logo = (
    <Box component="a" href={homeAddress}
      onClick={(event: React.MouseEvent) => { event.preventDefault(); goHome(); }}
      sx={{ display: 'inline-flex', alignItems: 'center', borderRadius: 1 }}>
      <Box component="img"
        src={muiTheme.palette.mode === 'dark' ? '/admin/ui/faxbot_mini_banner_dark.png' : '/admin/ui/faxbot_mini_banner_light.png'}
        alt="Faxbot" sx={{ height: 34, borderRadius: 1 }} />
    </Box>
  );

  // Send a fax stays in view for people who may send.
  const sendAddress = pageAddress('faxes', 'send');
  const send: SendAction | undefined = visible.some((area) => area.id === 'faxes' && area.pages.some((page) => page.id === 'send'))
    ? { href: sendAddress, selected: pageKey === 'faxes/send', onOpen: () => navigateTo(sendAddress) }
    : undefined;

  const panel = route && (
    <NavPanel areas={visible} currentArea={shown?.area.id ?? ''} currentPage={shown?.page.id ?? ''} onNavigate={openPage}
      send={send} logo={logo}
      footer={<UserMenu client={client} me={me} onNavigate={navigate} onSignOut={onSignOut} onIdentityChanged={onIdentityChanged} />} />
  );

  return (
    <Box sx={{ display: 'flex', minHeight: '100vh' }}>
      {/* Phones: a slim bar with the menu button and Send a fax; the panel opens as a drawer. */}
      {isMobile && (
        <AppBar position="fixed" elevation={0} sx={{
          color: 'text.primary', backdropFilter: 'blur(10px)', borderBottom: '1px solid', borderColor: 'divider',
          background: muiTheme.palette.mode === 'dark' ? 'rgba(15, 15, 17, 0.9)' : 'rgba(255, 255, 255, 0.9)',
        }}>
          <Toolbar sx={{ minHeight: 56, px: 1 }}>
            <IconButton color="inherit" edge="start" onClick={() => setMobileOpen(true)} sx={{ mr: 1 }} aria-label="open navigation">
              <MenuIcon />
            </IconButton>
            {logo}
            {send && !mobileOpen && <SendFaxButton send={send} size="small" sx={{ ml: 'auto' }} />}
          </Toolbar>
        </AppBar>
      )}
      {isMobile ? (
        <Drawer anchor="left" open={mobileOpen} onClose={() => setMobileOpen(false)} ModalProps={{ keepMounted: false }}
          sx={{ '& .MuiDrawer-paper': { width: DRAWER_WIDTH, borderRadius: '0 16px 16px 0' } }}>
          {panel}
        </Drawer>
      ) : (
        <Drawer variant="permanent" sx={{ width: DRAWER_WIDTH, flexShrink: 0,
          '& .MuiDrawer-paper': { width: DRAWER_WIDTH, boxSizing: 'border-box' } }}>
          {panel}
        </Drawer>
      )}

      <Box component="main" sx={{ flex: 1, minWidth: 0, px: { xs: 1.5, sm: 2, md: 4 }, pt: { xs: 9, md: 3 }, pb: 4 }}>
        {me.can_enroll_owner && (
          <Box sx={{ mb: 2 }}>
            <OwnerEnrollment client={client} onEnrolled={() => {
              onIdentityChanged();
              // A new installation's next step after its first owner is choosing a fax provider.
              if (context.provider_view && !context.provider_view.active_outbound) navigate('setup');
            }} />
          </Box>
        )}
        {shown && (
          <>
            <PageBreadcrumbs area={shown.area} page={shown.page} onNavigate={openPage} />
            {moved && moved.address === shown.address && <MovedNotice was={moved.was} onClose={() => setMoved(null)} />}
            <Box key={pageKey}>
              {shown.page.render(pageContext)}
            </Box>
          </>
        )}
        {route && route.kind !== 'page' && first && (
          <AddressProblem kind={route.kind}
            home={{ label: first.pages[0].label, href: homeAddress, onOpen: goHome }} />
        )}
        {!route && <Alert severity="info">There is nothing in the console for this account yet.</Alert>}
      </Box>
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
