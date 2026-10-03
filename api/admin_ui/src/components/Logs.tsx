import { useEffect, useMemo, useRef, useState } from 'react';
import { Box, Card, CardContent, Typography, TextField, Button, CircularProgress, Table, TableHead, TableRow, TableCell, TableBody, TableContainer, Alert, Dialog, DialogTitle, DialogContent, DialogActions, FormControlLabel, Switch, MenuItem, Select, InputLabel, FormControl } from '@mui/material';
import AdminAPIClient, { configurationWriteRejected } from '../api/client';
import type { ConfigurationWriteReceipt, Settings } from '../api/types';

interface LogsProps { client: AdminAPIClient; }
type AuditSettingsSnapshot = {
  desiredEnabled: boolean;
  activeEnabled: boolean;
  meta: NonNullable<Settings['_meta']>;
};

function parseQueryTokens(input: string): { q: string; filters: Record<string,string> } {
  const parts = input.split(/\s+/).filter(Boolean);
  const filters: Record<string, string> = {};
  const free: string[] = [];
  for (const p of parts) {
    const [k, v] = p.split(':', 2);
    if (v) filters[k.toLowerCase()] = v;
    else free.push(p);
  }
  return { q: free.join(' '), filters };
}

function Logs({ client }: LogsProps) {
  const [query, setQuery] = useState('');
  const [eventFilter, setEventFilter] = useState('');
  const [since, setSince] = useState<string>('');
  const [limit, setLimit] = useState<number>(200);
  const [items, setItems] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [source, setSource] = useState<'ring'|'file'>('ring');
  const [fileLines, setFileLines] = useState<number>(2000);
  const [expandOpen, setExpandOpen] = useState(false);
  const [expandedRow, setExpandedRow] = useState<any | null>(null);
  const [wrap, setWrap] = useState<boolean>(false);
  const [sincePreset, setSincePreset] = useState<string>('');
  const [follow, setFollow] = useState<boolean>(false);
  const [enableBusy, setEnableBusy] = useState(false);
  const [enableNeedsReload, setEnableNeedsReload] = useState(false);
  const [enableError, setEnableError] = useState<string | null>(null);
  const [enableNotice, setEnableNotice] = useState<string | null>(null);
  const [enableReceipt, setEnableReceipt] = useState<ConfigurationWriteReceipt | null>(null);
  const [enableOutcome, setEnableOutcome] = useState<'none' | 'unconfirmed' | 'rejected' | 'confirmed'>('none');
  const [auditSnapshot, setAuditSnapshot] = useState<AuditSettingsSnapshot | null>(null);
  const enableFence = useRef(false);
  const configurationEpoch = useRef(0);

  const run = async () => {
    try {
      setLoading(true); setError(null);
      const { q, filters } = parseQueryTokens(query);
      const ev = eventFilter || filters['event'] || undefined;
      let sinceIso: string | undefined = undefined;
      if (sincePreset) {
        const now = new Date();
        const m = { '5m':5, '15m':15, '1h':60, '24h': 1440 } as Record<string, number>;
        const mins = m[sincePreset];
        if (mins) sinceIso = new Date(now.getTime() - mins*60000).toISOString();
      } else if (since) {
        const d = new Date(since);
        if (!isNaN(d.getTime())) sinceIso = d.toISOString();
      }
      let res;
      if (source === 'file') {
        res = await client.tailLogs({ q, event: ev, lines: fileLines });
      } else {
        res = await client.getLogs({ q, event: ev, since: sinceIso, limit });
      }
      let rows = res.items || [];
      // Apply key:value filters client-side too
      rows = rows.filter((r: any) => Object.entries(filters).every(([k,v]) => String((r[k] ?? '')).toLowerCase().includes(v.toLowerCase())));
      setItems(rows);
      return true;
    } catch (e: any) {
      setError(e?.message || 'Failed to load logs');
      return false;
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { run(); }, []);
  useEffect(() => {
    if (!follow) return;
    const id = setInterval(run, 2000);
    return () => clearInterval(id);
  }, [follow, query, eventFilter, sincePreset, since, limit, source, fileLines]);

  const columns = useMemo(() => ['ts','event','job_id','key_id','backend','status','error','to','from','path'], []);

  useEffect(() => {
    configurationEpoch.current += 1;
    enableFence.current = false;
    setEnableBusy(false);
    setEnableNeedsReload(false);
    setEnableError(null);
    setEnableNotice(null);
    setEnableReceipt(null);
    setEnableOutcome('none');
    setAuditSnapshot(null);
    return () => { configurationEpoch.current += 1; };
  }, [client]);

  const readAuditSettings = async (epoch: number): Promise<AuditSettingsSnapshot | null> => {
    // Bracket the separately authorized active read with canonical identities,
    // so a changed configuration cannot be presented as one coherent snapshot.
    const before = await client.getSettings();
    if (epoch !== configurationEpoch.current) return null;
    if (!before._meta?.desired_revision_id) throw new Error('No canonical desired revision was returned.');
    const active = await client.getConfig();
    if (epoch !== configurationEpoch.current) return null;
    const desired = await client.getSettings();
    if (epoch !== configurationEpoch.current) return null;
    if (!desired._meta?.desired_revision_id) throw new Error('No canonical desired revision was returned.');
    if (before._meta.desired_revision_id !== desired._meta?.desired_revision_id
        || before._meta.active_revision_id !== desired._meta?.active_revision_id
        || before._meta.generation !== desired._meta?.generation) {
      throw new Error('Settings changed during the audit read. Reload explicitly to review the current revision.');
    }
    if (typeof desired.security.audit_enabled !== 'boolean' || typeof active.audit_log_enabled !== 'boolean') {
      throw new Error('Desired and active audit logging values were not available.');
    }
    return { desiredEnabled: desired.security.audit_enabled, activeEnabled: active.audit_log_enabled, meta: desired._meta };
  };

  const reloadAuditSettings = async () => {
    if (enableFence.current) return;
    const epoch = configurationEpoch.current;
    enableFence.current = true;
    setEnableBusy(true);
    setEnableError(null);
    setAuditSnapshot(null);
    try {
      const current = await readAuditSettings(epoch);
      if (epoch !== configurationEpoch.current) return;
      if (!current) return;
      setAuditSnapshot(current);
      setEnableNeedsReload(false);
    } catch (e: any) {
      if (epoch === configurationEpoch.current) setEnableError(`Audit settings could not be reloaded. ${e?.message || ''} Reload explicitly, or sign in again, before another change.`);
    } finally {
      if (epoch === configurationEpoch.current) {
        enableFence.current = false;
        setEnableBusy(false);
      }
    }
  };

  const enableAuditLogging = async () => {
    if (enableFence.current || enableNeedsReload) return;
    const epoch = configurationEpoch.current;
    enableFence.current = true;
    setEnableBusy(true);
    setEnableError(null);
    setEnableNotice(null);
    setEnableReceipt(null);
    setEnableOutcome('none');
    setAuditSnapshot(null);
    let writeStarted = false;
    try {
      const desired = await client.getSettings();
      if (epoch !== configurationEpoch.current) return;
      if (!desired._meta?.desired_revision_id) throw new Error('No canonical desired revision was returned.');
      writeStarted = true;
      setEnableOutcome('unconfirmed');
      const receipt = await client.updateSettings({ audit_log_enabled: true, expected_revision_id: desired._meta.desired_revision_id });
      if (epoch !== configurationEpoch.current) return;
      setEnableReceipt(receipt);
      setEnableOutcome('confirmed');
      setEnableNeedsReload(true);
      setEnableNotice(receipt._meta.apply_state === 'pending_restart'
        ? `${receipt.changed ? 'Desired audit settings saved durably.' : 'Desired audit settings are unchanged.'} Pending changes require every worker to stop and the installation to restart. Confirm active logging in Settings afterward.`
        : receipt.changed ? 'Audit logging settings saved durably and active.' : 'Audit logging settings are unchanged and active.');
      try {
        const current = await readAuditSettings(epoch);
        if (epoch !== configurationEpoch.current) return;
        if (!current) return;
        setAuditSnapshot(current);
        setEnableNeedsReload(false);
        if (!await run() && epoch === configurationEpoch.current) setEnableError('Save confirmed; logs could not be reloaded. Refresh logs explicitly, or sign in again.');
      } catch {
        if (epoch === configurationEpoch.current) setEnableError('Save confirmed; audit settings could not be reloaded. Reload settings explicitly, or sign in again, before another change.');
      }
    } catch (e: any) {
      if (epoch !== configurationEpoch.current) return;
      if (writeStarted) {
        setEnableOutcome(configurationWriteRejected(e) ? 'rejected' : 'unconfirmed');
        setEnableNeedsReload(true);
        setEnableError((e?.message || '').includes('409')
          ? 'Settings changed after the desired revision loaded. Audit logging was not changed by this request. Reload settings explicitly before trying again.'
          : `${configurationWriteRejected(e) ? 'Save was rejected.' : 'Save was not confirmed.'} ${e?.message || ''} Reload settings to check the current configuration before another change.`);
      } else {
        setEnableError(`Audit settings could not be loaded; no save was attempted. ${e?.message || ''}`);
      }
    } finally {
      if (epoch === configurationEpoch.current) {
        enableFence.current = false;
        setEnableBusy(false);
      }
    }
  };

  return (
    <Box>
      <Box display="flex" justifyContent="space-between" alignItems="center" mb={3}>
        <Typography variant="h4">Logs</Typography>
        <Box display="flex" gap={1}>
          <Button variant="outlined" onClick={run} disabled={loading}>{loading ? <CircularProgress size={18} /> : 'Refresh'}</Button>
        </Box>
      </Box>

      <Card sx={{ mb: 2 }}>
        <CardContent>
          <Box display="grid" gridTemplateColumns="1fr 180px 200px 120px" gap={2}>
            <TextField label="Search (supports key:value)" value={query} onChange={(e)=>setQuery(e.target.value)} size="small" />
            <TextField label="Event" value={eventFilter} onChange={(e)=>setEventFilter(e.target.value)} size="small" placeholder="e.g., job_created" />
            <Box display="flex" gap={1}>
              <FormControl size="small" sx={{ minWidth: 120 }}>
                <InputLabel>Since</InputLabel>
                <Select label="Since" value={sincePreset} onChange={(e)=>setSincePreset(e.target.value as string)}>
                  <MenuItem value="">Custom</MenuItem>
                  <MenuItem value="5m">Last 5m</MenuItem>
                  <MenuItem value="15m">Last 15m</MenuItem>
                  <MenuItem value="1h">Last 1h</MenuItem>
                  <MenuItem value="24h">Last 24h</MenuItem>
                </Select>
              </FormControl>
              <TextField label="Custom" value={since} onChange={(e)=>setSince(e.target.value)} size="small" placeholder="e.g., 2025-09-12 14:00" disabled={!!sincePreset} />
            </Box>
            <TextField label="Limit" type="number" value={limit} onChange={(e)=>setLimit(parseInt(e.target.value||'200'))} size="small" />
          </Box>
          <Box mt={2} display="flex" alignItems="center" gap={2}>
            <Button variant="contained" onClick={run} disabled={loading}>Apply Filters</Button>
            <FormControlLabel control={<Switch checked={source==='file'} onChange={(e)=>setSource(e.target.checked?'file':'ring')} />} label="Use file tail" />
            {source==='file' && (
              <TextField label="Lines" type="number" size="small" value={fileLines} onChange={(e)=>setFileLines(parseInt(e.target.value||'2000'))} />
            )}
            <FormControlLabel control={<Switch checked={wrap} onChange={(e)=>setWrap(e.target.checked)} />} label="Wrap" />
            <FormControlLabel control={<Switch checked={follow} onChange={(e)=>setFollow(e.target.checked)} />} label="Follow" />
          </Box>
        </CardContent>
      </Card>

      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
      {enableNotice && <Alert severity={enableReceipt?._meta.apply_state === 'pending_restart' ? 'warning' : 'success'} sx={{ mb: 2 }}>{enableNotice}</Alert>}
      {enableOutcome === 'unconfirmed' && <Alert severity="warning" sx={{ mb: 2 }}>
        The earlier save remains unconfirmed. {auditSnapshot
          ? 'The desired and active values below describe the currently loaded configuration; they do not establish whether that earlier request committed. Review them before making another change.'
          : 'Reload audit settings explicitly to inspect desired and active logging before making another change.'}
      </Alert>}
      {auditSnapshot && <Alert severity={auditSnapshot.meta.apply_state === 'pending_restart' ? 'warning' : 'info'} sx={{ mb: 2 }}>
        <Typography variant="body2">Loaded desired audit logging: {auditSnapshot.desiredEnabled ? 'enabled' : 'disabled'}. Loaded active audit logging: {auditSnapshot.activeEnabled ? 'enabled' : 'disabled'}.</Typography>
        <Typography variant="body2">Desired revision: {auditSnapshot.meta.desired_revision_id}. Active revision: {auditSnapshot.meta.active_revision_id}. Generation: {auditSnapshot.meta.generation}.</Typography>
        {auditSnapshot.meta.apply_state === 'pending_restart' && <Typography variant="body2">The loaded configuration has pending changes. Active values continue serving until every worker stops and the installation restarts.</Typography>}
        {auditSnapshot.desiredEnabled && <Typography variant="body2">Desired audit logging is already enabled; no further enable write is needed.</Typography>}
      </Alert>}
      {enableError && <Alert severity="warning" sx={{ mb: 2 }}>{enableError}</Alert>}
      {enableNeedsReload && <Button variant="outlined" onClick={reloadAuditSettings} disabled={enableBusy} sx={{ mb: 2 }}>Reload audit settings</Button>}
      {(!error && items.length === 0) && (
        <Alert severity="info" sx={{ mb: 2 }}>
          No logs yet. Enable audit logging in Settings → Security (AUDIT_LOG_ENABLED), then use the app and refresh.
          <Button size="small" variant="outlined" sx={{ ml: 2 }} onClick={enableAuditLogging} disabled={enableBusy || enableNeedsReload || auditSnapshot?.desiredEnabled}>Enable Now</Button>
        </Alert>
      )}

      <Card>
        <CardContent>
          <TableContainer sx={{ maxHeight: 520 }}>
            <Table stickyHeader size="small">
              <TableHead>
                <TableRow>
                  {columns.map(col => (<TableCell key={col}>{col.toUpperCase()}</TableCell>))}
                </TableRow>
              </TableHead>
              <TableBody>
                {items.length === 0 ? (
                  <TableRow><TableCell colSpan={columns.length}><Typography variant="body2" color="text.secondary">No matching logs</Typography></TableCell></TableRow>
                ) : items.map((row, idx) => (
                  <TableRow key={idx} hover onClick={()=>{ setExpandedRow(row); setExpandOpen(true); }} sx={{ cursor: 'pointer' }}>
                    {columns.map(col => (
                      <TableCell key={col} sx={{ maxWidth: wrap? 'none': 340, whiteSpace: wrap? 'normal':'nowrap', overflow: wrap? 'visible':'hidden', textOverflow: wrap? 'clip':'ellipsis' }}>
                        {row[col] !== undefined ? String(row[col]) : ''}
                      </TableCell>
                    ))}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableContainer>
          <Box mt={1}>
            <Typography variant="caption" color="text.secondary">Tips: use key:value filters like "event:job_failed backend:sinch" and free-text search to refine results.</Typography>
          </Box>
        </CardContent>
      </Card>

      <Dialog open={expandOpen} onClose={()=>setExpandOpen(false)} maxWidth="md" fullWidth>
        <DialogTitle>Log Entry</DialogTitle>
        <DialogContent>
          <Box component="pre" sx={{ p: 2, bgcolor: 'background.default', borderRadius: 1, whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontFamily: 'monospace', fontSize: '0.85rem' }}>
            {expandedRow ? JSON.stringify(expandedRow, null, 2) : ''}
          </Box>
        </DialogContent>
        <DialogActions>
          <Button onClick={()=>{ if (!expandedRow) return; const blob = new Blob([JSON.stringify(expandedRow, null, 2)], { type: 'application/json' }); const url = URL.createObjectURL(blob); const a = document.createElement('a'); a.href = url; a.download = 'log.json'; document.body.appendChild(a); a.click(); document.body.removeChild(a); URL.revokeObjectURL(url); }}>Download JSON</Button>
          <Button onClick={()=>{ if (!expandedRow) return; navigator.clipboard.writeText(JSON.stringify(expandedRow, null, 2)); }}>Copy</Button>
          <Button onClick={()=>setExpandOpen(false)}>Close</Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}

export default Logs;
