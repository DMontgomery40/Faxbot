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
              <Button variant="contained" onClick={runAuthSmoke} disabled={busyAuth}>{busyAuth ? <CircularProgress size={18} /> : 'Run'}</Button>
              <Button onClick={clearAuth} disabled={busyAuth}>Clear</Button>
            </Box>
            <ConsoleBox lines={authLines} loading={busyAuth} />
          </PrettyBox>
        </Grid>
        {inboundEnabled && (
          <Grid item xs={12} md={6}>
            <PrettyBox title={`Inbound (${backend || 'backend'}): simulate and list`}>
              <Box display="flex" alignItems="center" gap={1} mb={1}>
                <TextField size="small" label="To (optional)" value={toNumber} onChange={(e)=>setToNumber(e.target.value)} sx={{ maxWidth: 260 }} />
                <Button variant="contained" onClick={runInboundSim} disabled={busyInbound}>{busyInbound ? <CircularProgress size={18} /> : 'Run'}</Button>
                <Button onClick={clearInbound} disabled={busyInbound}>Clear</Button>
              </Box>
              <ConsoleBox lines={inboundLines} loading={busyInbound} />
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
                <Button variant="outlined" onClick={runCallbacksInfo} disabled={busyInfo}>{busyInfo ? <CircularProgress size={18} /> : 'Show'}</Button>
                <Button onClick={clearInfo} disabled={busyInfo}>Clear</Button>
              </Box>
              <ConsoleBox lines={infoLines} loading={busyInfo} />
            </CardContent>
          </Card>
        </Grid>
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
