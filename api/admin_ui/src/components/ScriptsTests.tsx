import React, { useMemo, useState } from 'react';
import {
  Box,
  Button,
  Typography,
  Alert,
  Grid,
  CircularProgress,
  useTheme,
  useMediaQuery,
  Stack,
  Paper,
  Fade,
  IconButton,
  Tooltip,
  Chip,
} from '@mui/material';
import {
  PlayArrow as RunIcon,
  Clear as ClearIcon,
  Code as CodeIcon,
  VpnKey as KeyIcon,
  CallReceived as InboundIcon,
  Settings as SettingsIcon,
  ContentCopy as CopyIcon,
  Security as SecurityIcon,
  Terminal as TerminalIcon,
  CheckCircle as CheckIcon,
  Info as InfoIcon,
} from '@mui/icons-material';
import AdminAPIClient from '../api/client';
import { ResponsiveFormSection, ResponsiveTextField } from './common/ResponsiveFormFields';

interface Props {
  client: AdminAPIClient;
  docsBase?: string;
}

const ConsoleBox: React.FC<{ lines: string[]; loading?: boolean; title?: string }> = ({ lines, loading, title }) => {
  const theme = useTheme();
  const isMobile = useMediaQuery(theme.breakpoints.down('sm'));
  
  const handleCopy = () => {
    if (lines.length > 0) {
      navigator.clipboard.writeText(lines.join('\n'));
    }
  };

  return (
    <Paper
      elevation={0}
      sx={{
        bgcolor: theme.palette.mode === 'dark' ? '#0B0F14' : '#1e1e1e',
        border: '1px solid',
        borderColor: theme.palette.mode === 'dark' ? '#1f2937' : 'rgba(0,0,0,0.2)',
        borderRadius: 2,
        p: { xs: 1.5, sm: 2 },
        fontFamily: '"Cascadia Code", "JetBrains Mono", "Fira Code", Consolas, monospace',
        fontSize: isMobile ? '0.75rem' : '0.85rem',
        height: isMobile ? 150 : 200,
        overflowY: 'auto',
        position: 'relative',
        '&::-webkit-scrollbar': {
          width: 8,
        },
        '&::-webkit-scrollbar-track': {
          backgroundColor: 'rgba(255,255,255,0.05)',
        },
        '&::-webkit-scrollbar-thumb': {
          backgroundColor: 'rgba(255,255,255,0.2)',
          borderRadius: 4,
        },
      }}
    >
      {title && (
        <Box sx={{ 
          position: 'sticky', 
          top: 0, 
          bgcolor: 'inherit',
          borderBottom: '1px solid rgba(255,255,255,0.1)',
          mb: 1,
          pb: 0.5
        }}>
          <Typography variant="caption" sx={{ color: 'primary.main', fontWeight: 600 }}>
            {title}
          </Typography>
        </Box>
      )}
      
      {lines.length > 0 && (
        <Tooltip title="Copy output">
          <IconButton
            size="small"
            onClick={handleCopy}
            sx={{
              position: 'absolute',
              top: 8,
              right: 8,
              color: 'rgba(255,255,255,0.6)',
              '&:hover': {
                color: 'rgba(255,255,255,0.9)',
              }
            }}
          >
            <CopyIcon fontSize="small" />
          </IconButton>
        </Tooltip>
      )}
      
      {loading ? (
        <Box display="flex" alignItems="center" gap={1} sx={{ color: '#90caf9' }}>
          <CircularProgress size={16} sx={{ color: 'inherit' }} /> 
          <span>Running…</span>
        </Box>
      ) : null}
      
      {lines.map((l, i) => (
        <div 
          key={i} 
          style={{ 
            whiteSpace: 'pre-wrap', 
            wordBreak: 'break-word',
            color: l.startsWith('[✓]') ? '#7EE83F' : 
                   l.startsWith('[!]') || l.startsWith('[error]') ? '#FF7B72' :
                   l.startsWith('[i]') ? '#79C0FF' : '#C9D1D9',
            lineHeight: 1.4
          }}
        >
          {l}
        </div>
      ))}
      
      {!loading && lines.length === 0 && (
        <Typography variant="body2" sx={{ color: 'rgba(255,255,255,0.4)', fontStyle: 'italic' }}>
          Output will appear here...
        </Typography>
      )}
    </Paper>
  );
};

const ScriptsTests: React.FC<Props> = ({ client, docsBase }) => {
  const [error, setError] = useState<string>('');
  const [busyAuth, setBusyAuth] = useState<boolean>(false);
  const [busyInbound, setBusyInbound] = useState<boolean>(false);
  const [busyInfo, setBusyInfo] = useState<boolean>(false);
  const [authLines, setAuthLines] = useState<string[]>([]);
  const [inboundLines, setInboundLines] = useState<string[]>([]);
  const [infoLines, setInfoLines] = useState<string[]>([]);
  const [toNumber, setToNumber] = useState<string>('+15551234567');
  const [backend, setBackend] = useState<string>('');
  const [inboundEnabled, setInboundEnabled] = useState<boolean>(false);
  const [publicApiUrl, setPublicApiUrl] = useState<string>('');
  const [sipSecret, setSipSecret] = useState<string>('');
  const [actions, setActions] = useState<Array<{ id: string; label: string }>>([]);
  const [actionOutput, setActionOutput] = useState<Record<string, string>>({});
  const [activeActionTab, setActiveActionTab] = useState<string>('');

  const theme = useTheme();

  const docsUrl = useMemo(() => `${docsBase || 'https://dmontgomery40.github.io/Faxbot'}/development/scripts-and-tests.html`, [docsBase]);

  const pushAuth = (line: string) => setAuthLines((prev) => [...prev, line]);
  const clearAuth = () => setAuthLines([]);
  const pushInbound = (line: string) => setInboundLines((prev) => [...prev, line]);
  const clearInbound = () => setInboundLines([]);
  const pushInfo = (line: string) => setInfoLines((prev) => [...prev, line]);
  const clearInfo = () => setInfoLines([]);

  React.useEffect(() => {
    (async () => {
      try {
        const s = await client.getSettings();
        const b = (s as any)?.backend?.type || '';
        setBackend(b);
        setInboundEnabled(Boolean((s as any)?.inbound?.enabled));
        setPublicApiUrl(((s as any)?.security?.public_api_url) || '');
        // Load container actions
        try {
          const al = await (client as any).listActions?.();
          if (al?.enabled && Array.isArray(al.items)) {
            const filtered = (al.items as any[])
              .filter((a) => !a.backend || a.backend.includes('*') || a.backend.includes(b))
              .map((a) => ({ id: a.id, label: a.label }));
            setActions(filtered);
            if (filtered.length > 0) setActiveActionTab(filtered[0].id);
          }
        } catch {}
      } catch (e: any) {
        setError(e?.message || 'Failed to load settings');
      }
    })();
  }, [client]);

  const runAuthSmoke = async () => {
    setError(''); clearAuth(); setBusyAuth(true);
    try {
      pushAuth('[i] Creating send+read API key');
      const { token } = await client.createApiKey({ name: 'gui-smoke', owner: 'admin', scopes: ['fax:send','fax:read'] });
      pushAuth(`[i] Key minted (ends with …${(token||'').slice(-6)})`);
      pushAuth('[i] Sending test TXT');
      const blob = new Blob([`hello from Faxbot Admin Console — ${new Date().toISOString()}`], { type: 'text/plain' });
      const file = new File([blob], 'gui-smoke.txt', { type: 'text/plain' });
      const send = await client.sendFax(toNumber, file);
      pushAuth(`[✓] Queued: ${send.id} status=${send.status}`);
      pushAuth('[i] Fetching status…');
      try {
        const job = await (client as any).getJob(send.id);
        pushAuth(JSON.stringify(job, null, 2));
      } catch (e) {
        pushAuth('[!] Could not fetch admin job detail; showing basic result only');
      }
    } catch (e: any) {
      setError(e?.message || 'Auth smoke failed');
    } finally {
      setBusyAuth(false);
    }
  };

  const runInboundSim = async () => {
    setError(''); clearInbound(); setBusyInbound(true);
    try {
      pushInbound('[i] Simulating inbound (admin)');
      const res = await client.simulateInbound({ to: toNumber, pages: 1, status: 'received' });
      pushInbound(`[✓] Inbound created: ${res.id}`);
      pushInbound('[i] Listing inbound…');
      const list = await client.listInbound();
      pushInbound(`Count: ${list.length}`);
      const first = list.find((i: any)=> i.id === (res as any).id) || list[0];
      if (first) pushInbound(JSON.stringify(first, null, 2));
      else pushInbound('[!] Could not find the simulated item in list');
    } catch (e: any) {
      setError(e?.message || 'Inbound simulation failed (enable inbound and admin scopes)');
    } finally {
      setBusyInbound(false);
    }
  };

  const runCallbacksInfo = async () => {
    setError(''); clearInfo(); setBusyInfo(true);
    try {
      pushInfo('[i] Fetching configured inbound callbacks…');
      const info = await client.getInboundCallbacks();
      pushInfo(JSON.stringify(info, null, 2));
    } catch (e: any) {
      setError(e?.message || 'Failed to fetch callbacks');
    } finally { setBusyInfo(false); }
  };

  const generateSecret = () => {
    try {
      if (window.crypto && (window.crypto as any).getRandomValues) {
        const arr = new Uint8Array(24);
        window.crypto.getRandomValues(arr);
        return Array.from(arr).map(b => b.toString(16).padStart(2, '0')).join('');
      }
    } catch {}
    return Math.random().toString(36).slice(2) + Math.random().toString(36).slice(2);
  };

  const saveSipInboundSecret = async () => {
    setError(''); pushInfo('[i] Saving Asterisk inbound secret and enabling inbound…'); setBusyInfo(true);
    try {
      const secret = sipSecret || generateSecret();
      setSipSecret(secret);
      await (client as any).updateSettings?.({ inbound_enabled: true, asterisk_inbound_secret: secret });
      const s = await client.getSettings();
      setInboundEnabled(Boolean((s as any)?.inbound?.enabled));
      pushInfo('[✓] Saved. Inbound is enabled. Update your dialplan to post with X-Internal-Secret.');
    } catch (e:any) {
      setError(e?.message || 'Failed to save inbound secret');
    } finally { setBusyInfo(false); }
  };

  const savePhaxioCallback = async () => {
    if (!publicApiUrl) { setError('PUBLIC_API_URL is not set. Configure it in Settings.'); return; }
    setError(''); pushInfo('[i] Saving PHAXIO_CALLBACK_URL from PUBLIC_API_URL…'); setBusyInfo(true);
    try {
      const url = `${publicApiUrl.replace(/\/$/, '')}/phaxio-callback`;
      await (client as any).updateSettings?.({ phaxio_status_callback_url: url });
      pushInfo(`[✓] Set callback URL to ${url}`);
    } catch (e:any) {
      setError(e?.message || 'Failed to save callback URL');
    } finally { setBusyInfo(false); }
  };

  return (
    <Box sx={{ p: { xs: 2, sm: 0 } }}>
      <Box 
        sx={{ 
          display: 'flex', 
          justifyContent: 'space-between', 
          alignItems: { xs: 'flex-start', sm: 'center' },
          flexDirection: { xs: 'column', sm: 'row' },
          gap: 2,
          mb: 3
        }}
      >
        <Box>
          <Typography variant="h4" component="h1" gutterBottom>
            Scripts & Tests
          </Typography>
          <Typography variant="body2" color="text.secondary">
            Run fax tests and backend-specific scripts
          </Typography>
        </Box>
      </Box>

      <Alert 
        severity="info" 
        icon={<InfoIcon />}
        sx={{ mb: 3, borderRadius: 2 }}
      >
        <Stack spacing={0.5}>
          <Typography variant="body2">
            These buttons run the same flows as our helper scripts directly from your browser — no terminal needed.
          </Typography>
          <Typography variant="body2">
            Learn more in the docs: <a href={docsUrl} target="_blank" rel="noreferrer">Scripts & Tests</a>.
          </Typography>
        </Stack>
      </Alert>

      {error && (
        <Fade in>
          <Alert severity="error" sx={{ mb: 2, borderRadius: 2 }} onClose={() => setError('')}>
            {error}
          </Alert>
        </Fade>
      )}

      <Grid container spacing={3}>
        {/* Auth Smoke Test */}
        <Grid item xs={12} lg={6}>
          <ResponsiveFormSection
            title="Auth Smoke Test"
            subtitle="Create key → send test → check status"
            icon={<KeyIcon />}
          >
            <Stack spacing={2}>
              <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2}>
                <ResponsiveTextField
                  label="Test to number" 
                  value={toNumber} 
                  onChange={setToNumber}
                  placeholder="+15551234567"
                  icon={<CodeIcon />}
                />
                <Stack direction="row" spacing={1}>
                  <Button 
                    variant="contained" 
                    onClick={runAuthSmoke} 
                    disabled={busyAuth || busyInbound || busyInfo}
                    startIcon={busyAuth ? <CircularProgress size={16} /> : <RunIcon />}
                    sx={{ borderRadius: 2, minWidth: 80 }}
                  >
                    {busyAuth ? 'Running' : 'Run'}
                  </Button>
                  <Button 
                    variant="outlined"
                    onClick={clearAuth} 
                    disabled={busyAuth || busyInbound || busyInfo}
                    startIcon={<ClearIcon />}
                    sx={{ borderRadius: 2 }}
                  >
                    Clear
                  </Button>
                </Stack>
              </Stack>
              <ConsoleBox lines={authLines} loading={busyAuth} />
            </Stack>
          </ResponsiveFormSection>
        </Grid>

        {/* Inbound Simulation */}
        {inboundEnabled && (
          <Grid item xs={12} lg={6}>
            <ResponsiveFormSection
              title={`Inbound (${backend || 'backend'})`}
              subtitle="Simulate and list inbound faxes"
              icon={<InboundIcon />}
            >
              <Stack spacing={2}>
                <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2}>
                  <ResponsiveTextField
                    label="To number (optional)" 
                    value={toNumber} 
                    onChange={setToNumber}
                    placeholder="+15551234567"
                  />
                  <Stack direction="row" spacing={1}>
                    <Button 
                      variant="contained" 
                      onClick={runInboundSim} 
                      disabled={busyInbound || busyAuth || busyInfo}
                      startIcon={busyInbound ? <CircularProgress size={16} /> : <RunIcon />}
                      sx={{ borderRadius: 2, minWidth: 80 }}
                    >
                      {busyInbound ? 'Running' : 'Run'}
                    </Button>
                    <Button 
                      variant="outlined"
                      onClick={clearInbound} 
                      disabled={busyInbound || busyAuth || busyInfo}
                      startIcon={<ClearIcon />}
                      sx={{ borderRadius: 2 }}
                    >
                      Clear
                    </Button>
                  </Stack>
                </Stack>
                <ConsoleBox lines={inboundLines} loading={busyInbound} />
              </Stack>
            </ResponsiveFormSection>
          </Grid>
        )}

        {/* Backend-specific helpers */}
        {backend === 'sip' && (
          <Grid item xs={12} lg={6}>
            <ResponsiveFormSection
              title="SIP/Asterisk: Inbound Secret"
              subtitle="Set a strong secret for dialplan authentication"
              icon={<SecurityIcon />}
            >
              <Stack spacing={2}>
                <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2}>
                  <ResponsiveTextField
                    label="ASTERISK_INBOUND_SECRET" 
                    value={sipSecret} 
                    onChange={setSipSecret}
                    type="password"
                  />
                  <Stack direction="row" spacing={1}>
                    <Button 
                      variant="outlined"
                      onClick={() => setSipSecret(generateSecret())} 
                      disabled={busyAuth || busyInbound || busyInfo}
                      sx={{ borderRadius: 2 }}
                    >
                      Generate
                    </Button>
                    <Button 
                      variant="contained" 
                      onClick={saveSipInboundSecret} 
                      disabled={busyInfo || busyAuth || busyInbound}
                      startIcon={busyInfo ? <CircularProgress size={16} /> : <CheckIcon />}
                      sx={{ borderRadius: 2 }}
                    >
                      {busyInfo ? 'Saving' : 'Enable & Save'}
                    </Button>
                  </Stack>
                </Stack>
                <Alert severity="info" sx={{ borderRadius: 2 }}>
                  <Typography variant="caption">
                    Dialplan should POST to /_internal/asterisk/inbound with header X-Internal-Secret.
                  </Typography>
                </Alert>
              </Stack>
            </ResponsiveFormSection>
          </Grid>
        )}

        {backend === 'phaxio' && (
          <Grid item xs={12} lg={6}>
            <ResponsiveFormSection
              title="Phaxio: Set Callback URL"
              subtitle="Configure webhook endpoint for status updates"
              icon={<SettingsIcon />}
            >
              <Stack spacing={2}>
                <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2}>
                  <ResponsiveTextField
                    label="PUBLIC_API_URL" 
                    value={publicApiUrl} 
                    onChange={setPublicApiUrl}
                    placeholder="https://api.example.com"
                  />
                  <Button 
                    variant="contained" 
                    onClick={savePhaxioCallback} 
                    disabled={busyInfo || busyAuth || busyInbound}
                    startIcon={busyInfo ? <CircularProgress size={16} /> : <CheckIcon />}
                    sx={{ borderRadius: 2, minWidth: 100 }}
                  >
                    {busyInfo ? 'Saving' : 'Save'}
                  </Button>
                </Stack>
                <Alert severity="warning" sx={{ borderRadius: 2 }}>
                  <Typography variant="caption">
                    Ensure this is HTTPS and publicly reachable. Configure signature verification in Settings if required.
                  </Typography>
                </Alert>
              </Stack>
            </ResponsiveFormSection>
          </Grid>
        )}

        {/* Inbound Callbacks Info */}
        <Grid item xs={12}>
          <ResponsiveFormSection
            title={backend === 'phaxio' ? 'Phaxio Inbound Callback' : 
                   backend === 'sinch' ? 'Sinch Inbound Callback' : 
                   backend === 'sip' ? 'Asterisk Inbound (internal)' : 'Inbound Callback'}
            subtitle="View current callback configuration"
            icon={<InfoIcon />}
          >
            <Stack spacing={2}>
              <Stack direction="row" spacing={1}>
                <Button 
                  variant="contained" 
                  onClick={runCallbacksInfo} 
                  disabled={busyInfo || busyAuth || busyInbound}
                  startIcon={busyInfo ? <CircularProgress size={16} /> : <InfoIcon />}
                  sx={{ borderRadius: 2 }}
                >
                  {busyInfo ? 'Loading' : 'Show Config'}
                </Button>
                <Button 
                  variant="outlined"
                  onClick={clearInfo} 
                  disabled={busyInfo || busyAuth || busyInbound}
                  startIcon={<ClearIcon />}
                  sx={{ borderRadius: 2 }}
                >
                  Clear
                </Button>
              </Stack>
              <ConsoleBox lines={infoLines} loading={busyInfo} />
            </Stack>
          </ResponsiveFormSection>
        </Grid>

        {/* Container Checks */}
        {actions.length > 0 && (
          <Grid item xs={12}>
            <ResponsiveFormSection
              title="Container Checks"
              subtitle="Run diagnostic scripts in the container"
              icon={<TerminalIcon />}
            >
              <Stack spacing={2}>
                <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap' }}>
                  {actions.map((a) => (
                    <Chip
                      key={a.id}
                      label={a.label}
                      onClick={async () => {
                        setActiveActionTab(a.id);
                        setBusyInfo(true);
                        try {
                          const r = await (client as any).runAction?.(a.id);
                          setActionOutput((prev) => ({ 
                            ...prev, 
                            [a.id]: (r?.stdout || '') + (r?.stderr ? "\n[stderr]\n" + r.stderr : '') 
                          }));
                        } catch (e: any) {
                          setActionOutput((prev) => ({ ...prev, [a.id]: e?.message || 'Failed' }));
                        } finally { 
                          setBusyInfo(false); 
                        }
                      }}
                      variant={activeActionTab === a.id ? "filled" : "outlined"}
                      color={activeActionTab === a.id ? "primary" : "default"}
                      sx={{ 
                        cursor: 'pointer',
                        borderRadius: 2,
                        '&:hover': {
                          backgroundColor: theme.palette.action.hover,
                        }
                      }}
                      disabled={busyAuth || busyInbound || busyInfo}
                    />
                  ))}
                </Box>
                
                {activeActionTab && (
                  <ConsoleBox 
                    lines={(actionOutput[activeActionTab]?.split('\n') || []).slice(0, 400)} 
                    loading={busyInfo && activeActionTab === actions.find(a => !actionOutput[a.id])?.id}
                    title={actions.find(a => a.id === activeActionTab)?.label}
                  />
                )}
              </Stack>
            </ResponsiveFormSection>
          </Grid>
        )}
      </Grid>
      
      <Alert 
        severity="info" 
        icon={false}
        sx={{ mt: 3, borderRadius: 2 }}
      >
        <Typography variant="caption" color="text.secondary">
          <strong>Tip:</strong> For cloud providers, set PUBLIC_API_URL and enable HTTPS when exposing the API. 
          The GUI tests respect current server settings (e.g., FAX_DISABLED for simulation).
        </Typography>
      </Alert>
    </Box>
  );
};

export default ScriptsTests;