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
  Chip,
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
  const [busy, setBusy] = useState<string>('');
  const [consoleLines, setConsoleLines] = useState<string[]>([]);
  const [toNumber, setToNumber] = useState<string>('+15551234567');

  const docsUrl = useMemo(() => `${docsBase || 'https://dmontgomery40.github.io/Faxbot'}/development/scripts-and-tests.html`, [docsBase]);

  const push = (line: string) => setConsoleLines((prev) => [...prev, line]);
  const clear = () => setConsoleLines([]);

  const runAuthSmoke = async () => {
    setError(''); clear(); setBusy('auth');
    try {
      push('[i] Creating send+read API key');
      const { token } = await client.createApiKey({ name: 'gui-smoke', owner: 'admin', scopes: ['fax:send','fax:read'] });
      push(`[i] Key minted (ends with …${(token||'').slice(-6)})`);
      push('[i] Sending test TXT');
      const blob = new Blob([`hello from Faxbot Admin Console — ${new Date().toISOString()}`], { type: 'text/plain' });
      const file = new File([blob], 'gui-smoke.txt', { type: 'text/plain' });
      const send = await client.sendFax(toNumber, file);
      push(`[✓] Queued: ${send.id} status=${send.status}`);
      // Fetch status via admin view for richer details
      push('[i] Fetching status…');
      try {
        const job = await (client as any).getJob(send.id);
        push(JSON.stringify(job, null, 2));
      } catch (e) {
        push('[!] Could not fetch admin job detail; showing basic result only');
      }
    } catch (e: any) {
      setError(e?.message || 'Auth smoke failed');
    } finally {
      setBusy('');
    }
  };

  const runInboundSim = async () => {
    setError(''); clear(); setBusy('inbound');
    try {
      push('[i] Simulating inbound (admin)');
      const res = await client.simulateInbound({ to: toNumber, pages: 1, status: 'received' });
      push(`[✓] Inbound created: ${res.id}`);
      push('[i] Listing inbound…');
      const list = await client.listInbound();
      push(`Count: ${list.length}`);
      const first = list.find((i: any)=> i.id === (res as any).id) || list[0];
      if (first) push(JSON.stringify(first, null, 2));
      else push('[!] Could not find the simulated item in list');
    } catch (e: any) {
      setError(e?.message || 'Inbound simulation failed (enable inbound and admin scopes)');
    } finally {
      setBusy('');
    }
  };

  const runCallbacksInfo = async () => {
    setError(''); setBusy('callbacks');
    try {
      push('[i] Fetching configured inbound callbacks…');
      const info = await client.getInboundCallbacks();
      push(JSON.stringify(info, null, 2));
    } catch (e: any) {
      setError(e?.message || 'Failed to fetch callbacks');
    } finally { setBusy(''); }
  };

  return (
    <Box>
      <Box display="flex" justifyContent="space-between" alignItems="center" mb={2}>
        <Typography variant="h4">Scripts & Tests</Typography>
        <Chip label="GUI‑first" color="primary" variant="outlined" />
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
              <Button variant="contained" onClick={runAuthSmoke} disabled={!!busy}>{busy==='auth' ? <CircularProgress size={18} /> : 'Run'}</Button>
              <Button onClick={()=>setConsoleLines([])} disabled={!!busy}>Clear</Button>
            </Box>
            <ConsoleBox lines={consoleLines} loading={busy==='auth'} />
          </PrettyBox>
        </Grid>
        <Grid item xs={12} md={6}>
          <PrettyBox title="Inbound: admin simulate and list">
            <Box display="flex" alignItems="center" gap={1} mb={1}>
              <TextField size="small" label="To (optional)" value={toNumber} onChange={(e)=>setToNumber(e.target.value)} sx={{ maxWidth: 260 }} />
              <Button variant="contained" onClick={runInboundSim} disabled={!!busy}>{busy==='inbound' ? <CircularProgress size={18} /> : 'Run'}</Button>
              <Button onClick={()=>setConsoleLines([])} disabled={!!busy}>Clear</Button>
            </Box>
            <ConsoleBox lines={consoleLines} loading={busy==='inbound'} />
          </PrettyBox>
        </Grid>
        <Grid item xs={12}>
          <Card>
            <CardContent>
              <Typography variant="h6" gutterBottom>Inbound Callbacks (configured URLs)</Typography>
              <Box display="flex" gap={1} mb={1}>
                <Button variant="outlined" onClick={runCallbacksInfo} disabled={!!busy}>{busy==='callbacks' ? <CircularProgress size={18} /> : 'Show'}</Button>
                <Button onClick={()=>setConsoleLines([])} disabled={!!busy}>Clear</Button>
              </Box>
              <ConsoleBox lines={consoleLines} loading={busy==='callbacks'} />
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
