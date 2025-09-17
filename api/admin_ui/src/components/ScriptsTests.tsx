import React, { useMemo, useState } from 'react';
import {
  Box,
  Button,
  Card,
  CardContent,
  Typography,
  Alert,
  Grid,
  TextField,
  CircularProgress,
  Divider,
} from '@mui/material';
import AdminAPIClient from '../api/client';

interface Props {
  client: AdminAPIClient;
  docsBase?: string;
}

function PrettyBox({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <Card sx={{ mb: 2 }}>
      <CardContent>
        <Typography variant="h6" sx={{ mb: 1 }}>{title}</Typography>
        {children}
      </CardContent>
    </Card>
  );
}

const ConsoleBox: React.FC<{ lines: string[]; loading?: boolean }>=({ lines, loading })=>{
  return (
    <Box sx={{
      bgcolor: 'background.default',
      border: '1px solid #1f2937',
      borderRadius: 1,
      p: 1.5,
      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace',
      fontSize: '0.85rem',
      height: 200,
      overflowY: 'auto'
    }}>
      {loading ? <Box display="flex" alignItems="center" gap={1}><CircularProgress size={16} /> Running…</Box> : null}
      {lines.map((l, i) => (
        <div key={i} style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>{l}</div>
      ))}
    </Box>
  );
};

const ScriptsTests: React.FC<Props> = ({ client, docsBase }) => {
  const [error, setError] = useState<string>('');
  // Per-card state to avoid interleaving logs or running simultaneously
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
      // Fetch status via admin view for richer details
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
    <Box>
      <Box display="flex" justifyContent="space-between" alignItems="center" mb={2}>
        <Typography variant="h4">Scripts & Tests</Typography>
      </Box>

      <Alert severity="info" sx={{ mb: 2 }}>
        These buttons run the same flows as our helper scripts directly from your browser — no terminal needed.
        Learn more in the docs: <a href={docsUrl} target="_blank" rel="noreferrer">Scripts & Tests</a>.
      </Alert>

      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}

      <Grid container spacing={2}>
        <Grid item xs={12} md={6}>
          <PrettyBox title="Auth Smoke: create key → send test → status">
            <Box display="flex" alignItems="center" gap={1} mb={1}>
              <TextField size="small" label="Test to" value={toNumber} onChange={(e)=>setToNumber(e.target.value)} sx={{ maxWidth: 260 }} />
              <Button variant="contained" onClick={runAuthSmoke} disabled={busyAuth || busyInbound || busyInfo}>{busyAuth ? <CircularProgress size={18} /> : 'Run'}</Button>
              <Button onClick={clearAuth} disabled={busyAuth || busyInbound || busyInfo}>Clear</Button>
            </Box>
            <ConsoleBox lines={authLines} loading={busyAuth} />
          </PrettyBox>
        </Grid>
        {inboundEnabled && (
          <Grid item xs={12} md={6}>
            <PrettyBox title={`Inbound (${backend || 'backend'}): simulate and list`}>
              <Box display="flex" alignItems="center" gap={1} mb={1}>
                <TextField size="small" label="To (optional)" value={toNumber} onChange={(e)=>setToNumber(e.target.value)} sx={{ maxWidth: 260 }} />
                <Button variant="contained" onClick={runInboundSim} disabled={busyInbound || busyAuth || busyInfo}>{busyInbound ? <CircularProgress size={18} /> : 'Run'}</Button>
                <Button onClick={clearInbound} disabled={busyInbound || busyAuth || busyInfo}>Clear</Button>
              </Box>
              <ConsoleBox lines={inboundLines} loading={busyInbound} />
            </PrettyBox>
          </Grid>
        )}

        {/* Backend-specific helpers */}
        {backend === 'sip' && (
          <Grid item xs={12} md={6}>
            <PrettyBox title="SIP/Asterisk: inbound secret">
              <Typography variant="body2" sx={{ mb: 1 }}>
                Set a strong secret used by your dialplan to call the internal inbound endpoint.
              </Typography>
              <Box display="flex" alignItems="center" gap={1} mb={1}>
                <TextField size="small" label="ASTERISK_INBOUND_SECRET" value={sipSecret} onChange={(e)=>setSipSecret(e.target.value)} sx={{ maxWidth: 320 }} />
                <Button onClick={()=>setSipSecret(generateSecret())} disabled={busyAuth || busyInbound || busyInfo}>Generate</Button>
                <Button variant="contained" onClick={saveSipInboundSecret} disabled={busyInfo || busyAuth || busyInbound}>{busyInfo ? <CircularProgress size={18} /> : 'Enable & Save'}</Button>
              </Box>
              <Typography variant="caption" color="text.secondary">
                Dialplan should POST to /_internal/asterisk/inbound with header X-Internal-Secret.
              </Typography>
            </PrettyBox>
          </Grid>
        )}
        {backend === 'phaxio' && (
          <Grid item xs={12} md={6}>
            <PrettyBox title="Phaxio: set callback URL">
              <Typography variant="body2" sx={{ mb: 1 }}>
                Uses PUBLIC_API_URL to set PHAXIO_CALLBACK_URL to <code>/phaxio-callback</code>.
              </Typography>
              <Box display="flex" alignItems="center" gap={1} mb={1}>
                <TextField size="small" label="PUBLIC_API_URL" value={publicApiUrl} onChange={(e)=>setPublicApiUrl(e.target.value)} sx={{ maxWidth: 420 }} />
                <Button variant="contained" onClick={savePhaxioCallback} disabled={busyInfo || busyAuth || busyInbound}>{busyInfo ? <CircularProgress size={18} /> : 'Save'}</Button>
              </Box>
              <Typography variant="caption" color="text.secondary">
                Ensure this is HTTPS and publicly reachable. Configure signature verification in Settings if required.
              </Typography>
            </PrettyBox>
          </Grid>
        )}
        <Grid item xs={12}>
          <Card>
            <CardContent>
              <Typography variant="h6" gutterBottom>
                {backend === 'phaxio' ? 'Phaxio Inbound Callback' : backend === 'sinch' ? 'Sinch Inbound Callback' : backend === 'sip' ? 'Asterisk Inbound (internal)' : 'Inbound Callback'}
              </Typography>
              <Box display="flex" gap={1} mb={1}>
                <Button variant="outlined" onClick={runCallbacksInfo} disabled={busyInfo || busyAuth || busyInbound}>{busyInfo ? <CircularProgress size={18} /> : 'Show'}</Button>
                <Button onClick={clearInfo} disabled={busyInfo || busyAuth || busyInbound}>Clear</Button>
              </Box>
              <ConsoleBox lines={infoLines} loading={busyInfo} />
            </CardContent>
          </Card>
        </Grid>

        {actions.length > 0 && (
          <Grid item xs={12}>
            <Card>
              <CardContent>
                <Typography variant="h6" gutterBottom>Container Checks</Typography>
                <Box display="flex" gap={1} flexWrap="wrap" mb={1}>
                  {actions.map((a) => (
                    <Button key={a.id} variant="outlined" size="small" disabled={busyAuth || busyInbound || busyInfo}
                      onClick={async ()=>{
                        setBusyInfo(true);
                        try {
                          const r = await (client as any).runAction?.(a.id);
                          setActionOutput((prev)=>({ ...prev, [a.id]: (r?.stdout||'') + (r?.stderr? "\n[stderr]\n"+r.stderr : '') }));
                        } catch (e:any) {
                          setActionOutput((prev)=>({ ...prev, [a.id]: e?.message || 'Failed' }));
                        } finally { setBusyInfo(false); }
                      }}
                    >{a.label}</Button>
                  ))}
                </Box>
                {actions.map((a)=> (
                  <Box key={a.id} sx={{ mb: 2 }}>
                    <Typography variant="subtitle2" sx={{ mb: 0.5 }}>{a.label}</Typography>
                    <ConsoleBox lines={(actionOutput[a.id]?.split('\n')||[]).slice(0,400)} loading={false} />
                  </Box>
                ))}
              </CardContent>
            </Card>
          </Grid>
        )}
      </Grid>
      <Divider sx={{ my: 2 }} />
      <Typography variant="body2" color="text.secondary">
        Tip: For cloud providers, set PUBLIC_API_URL and enable HTTPS when exposing the API. The GUI tests respect current server settings
        (e.g., FAX_DISABLED for simulation).
      </Typography>
    </Box>
  );
};

export default ScriptsTests;
