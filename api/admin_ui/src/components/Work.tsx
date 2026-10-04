// The Work screen: received documents with an owner, an acknowledgement target and a history.
import { useCallback, useEffect, useState } from 'react';
import {
  Alert, Box, Button, Card, CardContent, Chip, CircularProgress, Dialog, DialogActions, DialogContent, DialogTitle,
  Menu, MenuItem, Paper, Stack, Table, TableBody, TableCell, TableContainer, TableHead, TableRow, TextField,
  ToggleButton, ToggleButtonGroup, Typography, useMediaQuery, useTheme,
} from '@mui/material';
import RefreshIcon from '@mui/icons-material/Refresh';
import type AdminAPIClient from '../api/client';
import { formatServerTime } from '../api/time';
import type { InboundFax, WorkAssignee, WorkCounts, WorkItem, WorkView } from '../api/types';
import { deliveryErrorMessage } from './delivery/shared';
import WorkDetail from './work/WorkDetail';
import WorkSettingsPanel from './work/WorkSettingsPanel';
import { can, duplicateSentence, maskNumber, workStateSentence } from './work/text';

const VIEWS: Array<{ value: WorkView; label: string; count: keyof WorkCounts | null }> = [
  { value: 'mine', label: 'Mine', count: 'mine' },
  { value: 'unassigned', label: 'Unassigned', count: 'unassigned' },
  { value: 'overdue', label: 'Overdue', count: 'overdue' },
  { value: 'all', label: 'All', count: null },
];

const EMPTY: Record<WorkView, string> = {
  mine: 'Nothing is assigned to you.',
  unassigned: 'Every open document has an owner.',
  overdue: 'Nothing is overdue.',
  all: 'No received documents yet.',
};

function saveBlob(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}

function stateColor(item: WorkItem): 'default' | 'warning' | 'error' | 'success' | 'info' {
  if (item.state_key === 'overdue' || item.state_key === 'escalated') return 'error';
  if (item.state_key === 'waiting') return 'warning';
  if (item.state_key === 'done') return 'success';
  return 'info';
}

export interface WorkProps {
  client: AdminAPIClient;
  permissions: ReadonlySet<string>;
}

export default function Work({ client, permissions }: WorkProps) {
  const theme = useTheme();
  const isMobile = useMediaQuery(theme.breakpoints.down('md'));
  const [view, setView] = useState<WorkView>('all');
  const [items, setItems] = useState<WorkItem[] | null>(null);
  const [counts, setCounts] = useState<WorkCounts | null>(null);
  const [waiting, setWaiting] = useState<InboundFax[]>([]);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [selected, setSelected] = useState<WorkItem | null>(null);
  const [assigning, setAssigning] = useState<{ item: WorkItem; anchor: HTMLElement; people: WorkAssignee[] | null } | null>(null);
  const [finishing, setFinishing] = useState<WorkItem | null>(null);
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [listing, totals] = await Promise.all([client.listWork({ view }), client.workCounts()]);
      setItems(listing.items);
      setCounts(totals);
      setError(null);
    } catch (failure) {
      setError(failure);
    }
    try {
      const inbound = await client.listInbound();
      setWaiting(inbound.filter((fax) => fax.status === 'waiting' || fax.status === 'failed'));
    } catch {
      setWaiting([]);  // Received faxes are not listed for this person or inbound is off.
    }
  }, [client, view]);

  useEffect(() => { void load(); }, [load]);

  const act = async (label: string, change: () => Promise<WorkItem>) => {
    setBusy(label);
    try {
      const updated = await change();
      setNotice(workStateSentence(updated));
      await load();
    } catch (failure) {
      setError(failure);
      await load();
    } finally {
      setBusy(null);
    }
  };

  const openAssign = async (item: WorkItem, anchor: HTMLElement) => {
    setAssigning({ item, anchor, people: null });
    try {
      const { people } = await client.workAssignees(item.id);
      setAssigning((current) => (current && current.item.id === item.id ? { ...current, people } : current));
    } catch (failure) {
      setAssigning(null);
      setError(failure);
    }
  };

  const download = async (item: WorkItem) => {
    try {
      saveBlob(await client.downloadInboundPdf(item.inbound_fax_id), `document-${item.available_at.slice(0, 10)}.pdf`);
    } catch (failure) {
      setError(failure);
    }
  };

  const exportEvidence = async (item: WorkItem) => {
    try {
      saveBlob(await client.exportWork(item.id), `evidence-${item.available_at.slice(0, 10)}.zip`);
      setNotice('Evidence downloaded. The export is recorded in the item history.');
    } catch (failure) {
      setError(failure);
    }
  };

  const actions = (item: WorkItem) => (
    <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap onClick={(event) => event.stopPropagation()}>
      {can(item, 'assign') && (
        <Button size="small" onClick={(event) => void openAssign(item, event.currentTarget)}>
          {item.owner ? 'Reassign' : 'Assign'}
        </Button>
      )}
      {can(item, 'acknowledge') && (
        <Button size="small" variant="contained" disabled={busy !== null}
          onClick={() => void act('acknowledge', () => client.acknowledgeWork(item.id, item.version))}>Acknowledge</Button>
      )}
      {can(item, 'done') && (
        <Button size="small" onClick={() => { setNote(''); setFinishing(item); }}>Done</Button>
      )}
      {can(item, 'reopen') && (
        <Button size="small" onClick={() => void act('reopen', () => client.reopenWork(item.id, item.version))}>Reopen</Button>
      )}
      {can(item, 'export') && (
        <Button size="small" onClick={() => void exportEvidence(item)}>Export</Button>
      )}
    </Stack>
  );

  const where = (item: WorkItem) => item.mailbox ?? maskNumber(item.to_number);

  return (
    <Box>
      <Stack direction={{ xs: 'column', sm: 'row' }} justifyContent="space-between" alignItems={{ sm: 'center' }} spacing={2} sx={{ mb: 2 }}>
        <Box>
          <Typography variant="h5">Work</Typography>
          <Typography variant="body2" color="text.secondary">
            Each received document gets an owner who acknowledges it and marks it done.
          </Typography>
        </Box>
        <Button startIcon={<RefreshIcon />} onClick={() => void load()}>Refresh</Button>
      </Stack>

      {error ? <Alert severity="error" sx={{ mb: 2 }} onClose={() => setError(null)}>{deliveryErrorMessage(error)}</Alert> : null}
      {notice ? <Alert severity="success" sx={{ mb: 2 }} onClose={() => setNotice(null)}>{notice}</Alert> : null}

      <ToggleButtonGroup exclusive size="small" value={view} onChange={(_, next: WorkView | null) => next && setView(next)}
        aria-label="Which items" sx={{ mb: 2, flexWrap: 'wrap' }}>
        {VIEWS.map((option) => (
          <ToggleButton key={option.value} value={option.value}>
            {option.label}{option.count && counts ? ` (${counts[option.count]})` : ''}
          </ToggleButton>
        ))}
      </ToggleButtonGroup>

      {items === null && !error ? <CircularProgress size={24} /> : null}
      {items && items.length === 0 ? <Alert severity="info" sx={{ mb: 2 }}>{EMPTY[view]}</Alert> : null}

      {items && items.length > 0 && (isMobile ? (
        <Stack spacing={1.5} sx={{ mb: 3 }}>
          {items.map((item) => (
            <Card key={item.id} variant="outlined" onClick={() => setSelected(item)} sx={{ cursor: 'pointer' }}>
              <CardContent>
                <Chip size="small" color={stateColor(item)} label={workStateSentence(item)} sx={{ mb: 1, maxWidth: '100%' }} />
                <Typography variant="body2">From {maskNumber(item.from_number)} · {where(item)}</Typography>
                <Typography variant="caption" color="text.secondary">Arrived {formatServerTime(item.available_at)}</Typography>
                {duplicateSentence(item) && <Typography variant="caption" display="block">{duplicateSentence(item)}</Typography>}
                <Box sx={{ mt: 1 }}>{actions(item)}</Box>
              </CardContent>
            </Card>
          ))}
        </Stack>
      ) : (
        <TableContainer component={Paper} variant="outlined" sx={{ mb: 3 }}>
          <Table size="small" aria-label="Work items">
            <TableHead>
              <TableRow>
                <TableCell>From</TableCell>
                <TableCell>Mailbox or number</TableCell>
                <TableCell>Arrived</TableCell>
                <TableCell>Owner</TableCell>
                <TableCell>Due</TableCell>
                <TableCell>State</TableCell>
                <TableCell />
              </TableRow>
            </TableHead>
            <TableBody>
              {items.map((item) => (
                <TableRow key={item.id} hover onClick={() => setSelected(item)} sx={{ cursor: 'pointer' }}>
                  <TableCell>{maskNumber(item.from_number)}</TableCell>
                  <TableCell>{where(item)}</TableCell>
                  <TableCell>{formatServerTime(item.available_at)}</TableCell>
                  <TableCell>{item.owner?.name ?? '-'}</TableCell>
                  <TableCell>{item.due_at ? formatServerTime(item.due_at) : '-'}</TableCell>
                  <TableCell>
                    <Chip size="small" color={stateColor(item)} label={workStateSentence(item)} />
                    {duplicateSentence(item) && <Typography variant="caption" display="block">{duplicateSentence(item)}</Typography>}
                  </TableCell>
                  <TableCell align="right">{actions(item)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableContainer>
      ))}

      {waiting.length > 0 && (
        <Paper variant="outlined" sx={{ p: 2, mb: 3, borderRadius: 2 }}>
          <Typography variant="subtitle1">Waiting for documents</Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
            These faxes were announced but their documents have not arrived, so they have no owner yet.
          </Typography>
          <Table size="small" aria-label="Waiting for documents">
            <TableBody>
              {waiting.map((fax) => (
                <TableRow key={fax.id}>
                  <TableCell>From {maskNumber(fax.fr)}</TableCell>
                  <TableCell>To {maskNumber(fax.to)}</TableCell>
                  <TableCell>{formatServerTime(fax.received_at)}</TableCell>
                  <TableCell>{fax.status === 'failed' ? 'The document could not be fetched.' : 'Waiting for the document.'}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Paper>
      )}

      {permissions.has('settings:read') && <WorkSettingsPanel client={client} canWrite={permissions.has('settings:write')} />}

      <Menu anchorEl={assigning?.anchor} open={Boolean(assigning)} onClose={() => setAssigning(null)}>
        {assigning?.people === null && <MenuItem disabled>Loading…</MenuItem>}
        {assigning?.people?.length === 0 && <MenuItem disabled>Nobody else can see this document.</MenuItem>}
        {assigning?.people?.map((person) => (
          <MenuItem key={person.id} disabled={person.id === assigning.item.owner?.id} onClick={() => {
            const target = assigning.item;
            setAssigning(null);
            void act('assign', () => client.assignWork(target.id, person.id, target.version));
          }}>{person.name}</MenuItem>
        ))}
      </Menu>

      <Dialog open={Boolean(finishing)} onClose={() => setFinishing(null)} fullWidth maxWidth="sm">
        <DialogTitle>Mark done</DialogTitle>
        <DialogContent>
          <TextField autoFocus fullWidth label="What was done" value={note} inputProps={{ maxLength: 200 }}
            helperText="For example: Filed in the case system." onChange={(event) => setNote(event.target.value)} sx={{ mt: 1 }} />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setFinishing(null)}>Cancel</Button>
          <Button variant="contained" disabled={!note.trim() || busy !== null} onClick={() => {
            const target = finishing;
            setFinishing(null);
            if (target) void act('done', () => client.completeWork(target.id, note.trim(), target.version));
          }}>Mark done</Button>
        </DialogActions>
      </Dialog>

      <WorkDetail client={client} item={selected} onClose={() => setSelected(null)} onDownload={(item) => void download(item)} />
    </Box>
  );
}
