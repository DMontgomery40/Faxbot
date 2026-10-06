import { useEffect, useMemo, useRef, useState } from 'react';
import { Box, Card, CardContent, Typography, TextField, Button, CircularProgress, Table, TableHead, TableRow, TableCell, TableBody, TableContainer, Alert, Dialog, DialogTitle, DialogContent, DialogActions, FormControlLabel, Switch, MenuItem, Select, InputLabel, FormControl } from '@mui/material';
import AdminAPIClient, { configurationWriteRejected } from '../api/client';
import { formatServerTime } from '../api/time';
import type { ConfigurationWriteResult, Settings } from '../api/types';

interface LogsProps { client: AdminAPIClient; }
type AuditSettingsSnapshot = {
  desiredEnabled: boolean;
  activeEnabled: boolean;
  meta: NonNullable<Settings['_meta']>;
};

const sinceOptions = [
  { value: '', label: 'Custom' },
  { value: '5m', label: 'Last 5 minutes' },
  { value: '15m', label: 'Last 15 minutes' },
  { value: '1h', label: 'Last hour' },
  { value: '24h', label: 'Last 24 hours' },
];

// The columns, each with the name shown and the field it reads.
export const LOG_COLUMNS: Array<{ field: string; label: string }> = [
  { field: 'ts', label: 'Time' }, { field: 'event', label: 'Event' }, { field: 'job_id', label: 'Fax' },
  { field: 'key_id', label: 'Key' }, { field: 'backend', label: 'Provider' }, { field: 'status', label: 'Result' },
  { field: 'error', label: 'Error' }, { field: 'to', label: 'To' }, { field: 'from', label: 'From' },
];
// One value as people read it: the time in local time, yes or no, and nothing for an empty field.
export function logValue(field: string, value: unknown): string {
  if (value === undefined || value === null || value === '') return '';
  if (field === 'ts') return formatServerTime(String(value), String(value));
  if (typeof value === 'boolean') return value ? 'yes' : 'no';
  return typeof value === 'object' ? JSON.stringify(value) : String(value);
}

const COLUMN_FIELDS: Record<string, string> = Object.fromEntries(LOG_COLUMNS.map(({ field, label }) => [label.toLowerCase(), field]));

// "provider:sinch result:failed" matches one column each; other words search everything.
export function parseQueryTokens(input: string): { q: string; filters: Record<string,string> } {
  const parts = input.split(/\s+/).filter(Boolean);
  const filters: Record<string, string> = {};
  const free: string[] = [];
  for (const p of parts) {
    const [k, v] = p.split(':', 2);
    if (v) filters[COLUMN_FIELDS[k.toLowerCase()] ?? k.toLowerCase()] = v;
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
  // The entry exactly as recorded, for developers; the readable lines come first.
  const [showRaw, setShowRaw] = useState(false);
  const [wrap, setWrap] = useState<boolean>(false);
  const [sincePreset, setSincePreset] = useState<string>('');
  const [follow, setFollow] = useState<boolean>(false);
  const [enableBusy, setEnableBusy] = useState(false);
  const [enableNeedsReload, setEnableNeedsReload] = useState(false);
  const [enableError, setEnableError] = useState<string | null>(null);
  const [enableNotice, setEnableNotice] = useState<string | null>(null);
  const [enableResult, setEnableResult] = useState<ConfigurationWriteResult | null>(null);
  const [enableOutcome, setEnableOutcome] = useState<'none' | 'unconfirmed' | 'rejected' | 'confirmed'>('none');
  const [auditSnapshot, setAuditSnapshot] = useState<AuditSettingsSnapshot | null>(null);
  // Saved audit logging value that takes effect at the next restart, if any.
  const [pendingAudit, setPendingAudit] = useState<boolean | null>(null);
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

  const columns = useMemo(() => LOG_COLUMNS, []);

  useEffect(() => {
    configurationEpoch.current += 1;
    enableFence.current = false;
    setEnableBusy(false);
    setEnableNeedsReload(false);
    setEnableError(null);
    setEnableNotice(null);
    setEnableResult(null);
    setEnableOutcome('none');
    setAuditSnapshot(null);
    setPendingAudit(null);
    const epoch = configurationEpoch.current;
    // People who can read logs may not be allowed to read settings; without
    // settings the screen keeps its general prompt.
    client.getSettings().then((loaded) => {
      if (epoch !== configurationEpoch.current) return;
      const meta = loaded._meta;
      if (meta?.apply_state === 'pending_restart' && meta.pending_fields?.includes('audit_log_enabled')
          && typeof loaded.security?.audit_enabled === 'boolean') {
        setPendingAudit(loaded.security.audit_enabled);
      }
    }).catch(() => undefined);
    return () => { configurationEpoch.current += 1; };
  }, [client]);

  const readAuditSettings = async (epoch: number): Promise<AuditSettingsSnapshot | null> => {
    // Read settings before and after the active config so a concurrent change
    // is reported instead of mixing two configurations.
    const before = await client.getSettings();
    if (epoch !== configurationEpoch.current) return null;
    if (!before._meta?.desired_revision_id) throw new Error('Settings could not be read.');
    const active = await client.getConfig();
    if (epoch !== configurationEpoch.current) return null;
    const desired = await client.getSettings();
    if (epoch !== configurationEpoch.current) return null;
    if (!desired._meta?.desired_revision_id) throw new Error('Settings could not be read.');
    if (before._meta.desired_revision_id !== desired._meta?.desired_revision_id
        || before._meta.active_revision_id !== desired._meta?.active_revision_id
        || before._meta.generation !== desired._meta?.generation) {
      throw new Error('Settings changed while loading.');
    }
    if (typeof desired.security.audit_enabled !== 'boolean' || typeof active.audit_log_enabled !== 'boolean') {
      throw new Error('Whether events are recorded could not be read.');
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
    } catch {
      if (epoch === configurationEpoch.current) setEnableError("Couldn't read whether events are recorded. Reload to try again.");
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
    setEnableResult(null);
    setEnableOutcome('none');
    setAuditSnapshot(null);
    let writeStarted = false;
    try {
      const desired = await client.getSettings();
      if (epoch !== configurationEpoch.current) return;
      if (!desired._meta?.desired_revision_id) throw new Error('Settings could not be read.');
      writeStarted = true;
      setEnableOutcome('unconfirmed');
      const writeResult = await client.updateSettings({ audit_log_enabled: true, expected_revision_id: desired._meta.desired_revision_id });
      if (epoch !== configurationEpoch.current) return;
      setEnableResult(writeResult);
      setEnableOutcome('confirmed');
      setEnableNeedsReload(true);
      setEnableNotice(writeResult._meta.apply_state === 'pending_restart'
        ? `${writeResult.changed ? 'Event recording saved.' : 'Nothing changed.'} Restart Faxbot to apply pending changes.`
        : writeResult.changed ? 'Event recording is on.' : 'Event recording is already on.');
      try {
        const current = await readAuditSettings(epoch);
        if (epoch !== configurationEpoch.current) return;
        if (!current) return;
        setAuditSnapshot(current);
        setEnableNeedsReload(false);
        if (!await run() && epoch === configurationEpoch.current) setEnableError("Event recording saved, but the logs couldn't be refreshed. Select Refresh to try again.");
      } catch {
        if (epoch === configurationEpoch.current) setEnableError("Event recording saved, but its status couldn't be reloaded. Reload to check it.");
      }
    } catch (e: any) {
      if (epoch !== configurationEpoch.current) return;
      if (writeStarted) {
        setEnableOutcome(configurationWriteRejected(e) ? 'rejected' : 'unconfirmed');
        setEnableNeedsReload(true);
        setEnableError((e?.message || '').includes('409')
          ? 'Someone else changed these settings. Reload to see the current values, then try again.'
          : configurationWriteRejected(e)
            ? `Event recording couldn't be turned on. ${e?.message || ''}`.trim()
            : "Couldn't confirm the change. Reload to check whether events are recorded.");
      } else {
        setEnableError("Couldn't load the event recording settings. Nothing was changed.");
      }
    } finally {
      if (epoch === configurationEpoch.current) {
        enableFence.current = false;
        setEnableBusy(false);
      }
    }
  };

  const showEnable = !error && items.length === 0;
  const auditPending = auditSnapshot !== null && auditSnapshot.desiredEnabled !== auditSnapshot.activeEnabled;
  const auditStatus: { severity: 'success' | 'info' | 'warning'; text: string } | null =
    enableError ? { severity: 'warning', text: enableError }
    : enableNotice ? { severity: enableResult?._meta.apply_state === 'pending_restart' ? 'warning' : 'success', text: enableNotice }
    : enableBusy ? { severity: 'info', text: enableNeedsReload ? 'Checking event recording…' : 'Turning on event recording…' }
    : auditSnapshot ? (auditPending
      ? { severity: 'warning', text: auditSnapshot.desiredEnabled ? 'Event recording turns on when Faxbot restarts.' : 'Event recording turns off when Faxbot restarts.' }
      : { severity: 'info', text: auditSnapshot.activeEnabled ? 'Event recording is on.' : 'Event recording is off.' })
    : pendingAudit !== null && enableOutcome === 'none'
      ? { severity: 'warning', text: pendingAudit ? 'Event recording turns on when Faxbot restarts.' : 'Event recording turns off when Faxbot restarts.' }
    : enableOutcome === 'unconfirmed' ? { severity: 'warning', text: 'Reload to check whether events are recorded.' }
    : showEnable ? { severity: 'info', text: 'If event recording is off, turn it on to record new events.' }
    : enableNeedsReload ? { severity: 'info', text: 'Reload to check event recording.' }
    : null;

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
          <Box sx={{
            display: 'grid',
            gridTemplateColumns: { xs: 'minmax(0, 1fr)', sm: 'repeat(2, minmax(0, 1fr))', lg: 'minmax(0, 1fr) 160px minmax(380px, 1.4fr) 100px' },
            gap: 2,
          }}>
            <TextField label="Search" value={query} onChange={(e)=>setQuery(e.target.value)} size="small" placeholder="Words to find" />
            <TextField label="Event" value={eventFilter} onChange={(e)=>setEventFilter(e.target.value)} size="small" placeholder="The whole event name, such as job_failed" />
            <Box sx={{
              display: 'grid',
              gridTemplateColumns: { xs: 'minmax(0, 1fr)', md: '140px minmax(220px, 1fr)' },
              gridColumn: { sm: '1 / -1', lg: 'auto' },
              gap: 1,
            }}>
              <FormControl size="small">
                <InputLabel id="logs-since-label" shrink>Since</InputLabel>
                <Select
                  id="logs-since"
                  labelId="logs-since-label"
                  label="Since"
                  value={sincePreset}
                  onChange={(e)=>setSincePreset(e.target.value as string)}
                  displayEmpty
                  renderValue={(value) => sinceOptions.find(option => option.value === value)?.label ?? value}
                >
                  {sinceOptions.map(option => (
                    <MenuItem key={option.value} value={option.value}>{option.label}</MenuItem>
                  ))}
                </Select>
              </FormControl>
              <TextField label="Custom" value={since} onChange={(e)=>setSince(e.target.value)} size="small" placeholder="e.g., 2025-09-12 14:00" disabled={!!sincePreset} />
            </Box>
            <TextField label="Most entries" type="number" value={limit} onChange={(e)=>setLimit(parseInt(e.target.value||'200'))} size="small" />
          </Box>
          <Box sx={{
            mt: 2,
            display: 'flex',
            flexDirection: { xs: 'column', sm: 'row' },
            alignItems: { xs: 'flex-start', sm: 'center' },
            flexWrap: 'wrap',
            gap: { xs: 1, sm: 2 },
          }}>
            <Button variant="contained" onClick={run} disabled={loading}>Show matching entries</Button>
            <FormControlLabel control={<Switch checked={source==='file'} onChange={(e)=>setSource(e.target.checked?'file':'ring')} />} label="Read the log file" />
            {source==='file' && (
              <TextField label="Lines to read" type="number" size="small" value={fileLines} onChange={(e)=>setFileLines(parseInt(e.target.value||'2000'))} />
            )}
            <FormControlLabel control={<Switch checked={wrap} onChange={(e)=>setWrap(e.target.checked)} />} label="Wrap long lines" />
            <FormControlLabel control={<Switch checked={follow} onChange={(e)=>setFollow(e.target.checked)} />} label="Keep updating" />
          </Box>
        </CardContent>
      </Card>

      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
      {auditStatus && (
        <Alert severity={auditStatus.severity} sx={{ mb: 2 }}>
          <Box sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', columnGap: 2, rowGap: 1 }}>
            <Typography variant="body2">{auditStatus.text}</Typography>
            {showEnable && !enableNeedsReload && !auditSnapshot?.desiredEnabled && pendingAudit === null && (
              <Button size="small" variant="outlined" onClick={enableAuditLogging} disabled={enableBusy}>Turn on now</Button>
            )}
            {enableNeedsReload && (
              <Button size="small" variant="outlined" onClick={reloadAuditSettings} disabled={enableBusy}>Reload event settings</Button>
            )}
          </Box>
        </Alert>
      )}

      <Card>
        <CardContent>
          <TableContainer sx={{ maxHeight: 520 }}>
            <Table stickyHeader size="small">
              <TableHead>
                <TableRow>
                  {columns.map(col => (<TableCell key={col.field}>{col.label}</TableCell>))}
                </TableRow>
              </TableHead>
              <TableBody>
                {items.length === 0 ? (
                  <TableRow><TableCell colSpan={columns.length}><Typography variant="body2" color="text.secondary">No matching entries</Typography></TableCell></TableRow>
                ) : items.map((row, idx) => (
                  <TableRow key={idx} hover onClick={()=>{ setExpandedRow(row); setShowRaw(false); setExpandOpen(true); }} sx={{ cursor: 'pointer' }}>
                    {columns.map(col => (
                      <TableCell key={col.field} sx={{ maxWidth: wrap? 'none': 340, whiteSpace: wrap? 'normal':'nowrap', overflow: wrap? 'visible':'hidden', textOverflow: wrap? 'clip':'ellipsis' }}>
                        {logValue(col.field, row[col.field])}
                      </TableCell>
                    ))}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableContainer>
          <Box mt={1}>
            <Typography variant="caption" color="text.secondary">To search one column, type its name, a colon and the words, such as provider:sinch or result:failed. Select an entry to see all of it.</Typography>
          </Box>
        </CardContent>
      </Card>

      <Dialog open={expandOpen} onClose={()=>setExpandOpen(false)} maxWidth="md" fullWidth>
        <DialogTitle>Log entry</DialogTitle>
        <DialogContent>
          <Box data-testid="log-entry-lines">
            {expandedRow && LOG_COLUMNS.filter((col) => logValue(col.field, expandedRow[col.field])).map((col) => (
              <Box key={col.field} display="flex" gap={2} sx={{ py: 0.5 }}>
                <Typography variant="body2" color="text.secondary" sx={{ minWidth: 96 }}>{col.label}</Typography>
                <Typography variant="body2" sx={{ wordBreak: 'break-word' }}>{logValue(col.field, expandedRow[col.field])}</Typography>
              </Box>
            ))}
          </Box>
          <FormControlLabel sx={{ mt: 1 }} control={<Switch checked={showRaw} onChange={(event) => setShowRaw(event.target.checked)} />}
            label="Show all recorded details" />
          {showRaw && (
            <Box component="pre" data-testid="log-entry-raw" sx={{ p: 2, bgcolor: 'background.default', borderRadius: 1, whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontFamily: 'monospace', fontSize: '0.85rem' }}>
              {expandedRow ? JSON.stringify(expandedRow, null, 2) : ''}
            </Box>
          )}
        </DialogContent>
        <DialogActions>
          <Button onClick={()=>{ if (!expandedRow) return; const blob = new Blob([JSON.stringify(expandedRow, null, 2)], { type: 'application/json' }); const url = URL.createObjectURL(blob); const a = document.createElement('a'); a.href = url; a.download = 'log.json'; document.body.appendChild(a); a.click(); document.body.removeChild(a); URL.revokeObjectURL(url); }}>Download</Button>
          <Button onClick={()=>{ if (!expandedRow) return; navigator.clipboard.writeText(JSON.stringify(expandedRow, null, 2)); }}>Copy</Button>
          <Button onClick={()=>setExpandOpen(false)}>Close</Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}

export default Logs;
