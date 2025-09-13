import { useEffect, useMemo, useState } from 'react';
import { Box, Card, CardContent, Typography, TextField, Button, CircularProgress, Table, TableHead, TableRow, TableCell, TableBody, TableContainer, Alert, Dialog, DialogTitle, DialogContent, DialogActions, FormControlLabel, Switch, MenuItem, Select, InputLabel, FormControl } from '@mui/material';
import AdminAPIClient from '../api/client';

interface LogsProps { client: AdminAPIClient; }

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
    } catch (e: any) {
      setError(e?.message || 'Failed to load logs');
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
      {(!error && items.length === 0) && (
        <Alert severity="info" sx={{ mb: 2 }}>
          No logs yet. Enable audit logging in Settings → Security (AUDIT_LOG_ENABLED), then use the app and refresh.
          <Button size="small" variant="outlined" sx={{ ml: 2 }} onClick={async ()=>{ try { await (client as any).updateSettings?.({ audit_log_enabled: true }); await (client as any).reloadSettings?.(); await run(); } catch {} }}>Enable Now</Button>
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
