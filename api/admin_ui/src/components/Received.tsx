// Faxes → Received: every received fax and imported document in one list, with
// who owns it and its state, whether it was emailed, and what can be done next.
// This is the former Inbox and Work screens together.
import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  IconButton,
  Menu,
  MenuItem,
  Paper,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  TextField,
  ToggleButton,
  ToggleButtonGroup,
  Tooltip,
  Typography,
  useMediaQuery,
  useTheme,
} from '@mui/material';
import {
  CheckCircle as SuccessIcon,
  Download as DownloadIcon,
  Error as ErrorIcon,
  Info as InfoIcon,
  Refresh as RefreshIcon,
  Send as SendIcon,
  Sync as FetchIcon,
} from '@mui/icons-material';
import AdminAPIClient from '../api/client';
import { docsLink } from '../docsLinks';
import { parseServerTime } from '../api/time';
import type { EfaxStatus, InboundFax, WorkAssignee, WorkCounts, WorkItem, WorkView } from '../api/types';
import type { EmailConnector, IntakeItem } from '../api/deliveryTypes';
import type { AdminDestination } from '../navigation';
import {
  DeliveryStatusLine, DirectDeliveries, emailDeliveryApplies, inboundFaxStatus, isNewFax, providerName,
} from './delivery/InboxDelivery';
import type { DeliveryTone } from './delivery/InboxDelivery';
import { DeliveryError, Notice } from './delivery/shared';
import { InboundCostLine, useInboundCosts } from './delivery/FaxCost';
import InboundRecovery from './InboundRecovery';
import WorkDetail from './work/WorkDetail';
import WorkSettingsPanel from './work/WorkSettingsPanel';
import { can, duplicateSentence, workStateSentence } from './work/text';

// Which received faxes are listed. The first four follow the work queue's own views.
export type ReceivedFilter = 'all' | 'mine' | 'waiting' | 'overdue' | 'not-delivered';

export const RECEIVED_FILTERS: ReceivedFilter[] = ['all', 'mine', 'waiting', 'overdue', 'not-delivered'];

const FILTER_VIEW: Partial<Record<ReceivedFilter, WorkView>> = { mine: 'mine', waiting: 'unassigned', overdue: 'overdue' };

// The most work items the server lists at once; open ones come first.
const WORK_LIMIT = 200;

const EMPTY: Record<ReceivedFilter, string> = {
  all: 'No received faxes yet.',
  mine: 'Nothing is assigned to you.',
  waiting: 'Every received fax has an owner.',
  overdue: 'Nothing is overdue.',
  'not-delivered': 'Every email delivery went through.',
};

// One row: a received fax, its work item, or both.
interface Row {
  key: string;
  fax: InboundFax | null;
  work: WorkItem | null;
}

export function readFilter(value: string | null | undefined): ReceivedFilter {
  return RECEIVED_FILTERS.includes(value as ReceivedFilter) ? value as ReceivedFilter : 'all';
}

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

function workColor(item: WorkItem): 'default' | 'warning' | 'error' | 'success' | 'info' {
  if (item.state_key === 'overdue' || item.state_key === 'escalated') return 'error';
  if (item.state_key === 'waiting') return 'warning';
  if (item.state_key === 'done') return 'success';
  return 'info';
}

function maskPhoneNumber(phone?: string | null): string {
  // A number nobody reported is unknown, not hidden.
  if (!phone) return 'Unknown';
  if (phone.length < 4) return '****';
  return '*'.repeat(phone.length - 4) + phone.slice(-4);
}

function rowTime(row: Row): number {
  const value = row.fax ? (row.fax.source_received_at || row.fax.received_at) : row.work?.available_at;
  return parseServerTime(value)?.getTime() ?? 0;
}

function checkedSentence(status: EfaxStatus): string {
  const when = parseServerTime(status.checked_at);
  return when
    ? `Faxbot checks eFax for received faxes; it last checked at ${when.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}.`
    : 'Faxbot checks eFax for received faxes; it has not checked yet.';
}

interface ReceivedProps {
  client: AdminAPIClient;
  docsBase?: string;
  inboundEnabled?: boolean;
  onNavigate?: (destination: AdminDestination) => void;
  // Installation permissions from /auth/me. Actions are shown only to people
  // allowed to use them; the server checks again.
  permissions?: ReadonlySet<string>;
  // What the console says this person may list: received faxes, and the work queue.
  canList?: boolean;
  canWork?: boolean;
  // The filter in the page address (?show=waiting), and a way to keep it there.
  show?: ReceivedFilter;
  onShowChange?: (show: ReceivedFilter) => void;
  // Opens Send a fax; absent for people who may not send.
  onSendFax?: () => void;
}

export default function Received({
  client, docsBase, inboundEnabled, onNavigate, permissions, canList = true, canWork = true, show = 'all', onShowChange, onSendFax,
}: ReceivedProps) {
  const canReadProviders = !!permissions?.has('providers:read');
  const canChangeProviders = !!permissions?.has('providers:write');
  // Email delivery status comes from the installation's intake queue; accounts
  // that cannot read it see the list without it.
  const canReadDelivery = !!permissions?.has('mailboxes:read');
  const canRetryDelivery = !!permissions?.has('settings:write');
  const canOpenEmailSettings = !!permissions?.has('settings:read') && !!onNavigate;
  const receiving = inboundEnabled !== false;

  const theme = useTheme();
  const isMobile = useMediaQuery(theme.breakpoints.down('md'));
  const isSmallMobile = useMediaQuery(theme.breakpoints.down('sm'));

  const [filter, setFilter] = useState<ReceivedFilter>(show);
  useEffect(() => { setFilter(show); }, [show]);

  const [faxes, setFaxes] = useState<InboundFax[]>([]);
  const [work, setWork] = useState<WorkItem[] | null>(null);
  const [counts, setCounts] = useState<WorkCounts | null>(null);
  const [viewIds, setViewIds] = useState<Set<string> | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [hasLoaded, setHasLoaded] = useState(false);
  const [deliveries, setDeliveries] = useState<IntakeItem[] | null>(null);
  const [notDelivered, setNotDelivered] = useState<number | null>(null);
  const [connectors, setConnectors] = useState<EmailConnector[] | null>(null);
  const [callbacks, setCallbacks] = useState<any | null>(null);
  const [efax, setEfax] = useState<EfaxStatus | null>(null);
  const [fetchingId, setFetchingId] = useState<string | null>(null);
  const [retrying, setRetrying] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [selected, setSelected] = useState<WorkItem | null>(null);
  const [assigning, setAssigning] = useState<{ item: WorkItem; anchor: HTMLElement; people: WorkAssignee[] | null } | null>(null);
  const [finishing, setFinishing] = useState<WorkItem | null>(null);
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState<string | null>(null);
  const costs = useInboundCosts(client, faxes);

  const fetchList = useCallback(async () => {
    const view = FILTER_VIEW[filter];
    const [faxResult, workResult, countResult, viewResult] = await Promise.allSettled([
      receiving && canList ? client.listInbound() : Promise.resolve([] as InboundFax[]),
      canWork ? client.listWork({ view: 'all', limit: WORK_LIMIT }) : Promise.resolve(null),
      canWork ? client.workCounts() : Promise.resolve(null),
      canWork && view ? client.listWork({ view, limit: WORK_LIMIT }) : Promise.resolve(null),
    ]);
    if (faxResult.status === 'fulfilled') setFaxes(faxResult.value);
    // Without the work queue the list still shows every received fax, without owners.
    setWork(workResult.status === 'fulfilled' && workResult.value ? workResult.value.items : null);
    setCounts(countResult.status === 'fulfilled' ? countResult.value : null);
    setViewIds(viewResult.status === 'fulfilled' && viewResult.value ? new Set(viewResult.value.items.map((item) => item.id)) : null);
    const listed = faxResult.status === 'fulfilled' || workResult.status === 'fulfilled';
    setHasLoaded(listed);
    setLoadError(listed ? null : "Received faxes couldn't be loaded. Select Refresh to try again.");
    setLoading(false);
  }, [client, receiving, canList, canWork, filter]);

  const fetchDeliveries = useCallback(async () => {
    if (!receiving || !canReadDelivery) {
      setDeliveries(null);
      setNotDelivered(null);
      return;
    }
    try {
      const result = await client.listIntakeItems({ limit: 500 });
      setDeliveries(result.items);
      setNotDelivered(result.counts?.failed ?? null);
    } catch {
      setDeliveries(null);
      setNotDelivered(null);
    }
    try {
      setConnectors((await client.listEmailConnectors()).connectors);
    } catch {
      setConnectors(null);
    }
  }, [client, receiving, canReadDelivery]);

  // How received faxes reach Faxbot: one line for the trunk or eFax.
  const fetchReceiving = useCallback(async () => {
    if (!receiving || !canReadProviders) {
      setCallbacks(null);
      setEfax(null);
      return;
    }
    let found: any = null;
    try {
      found = await client.getInboundCallbacks();
    } catch {
      found = null;  // the list works without this line
    }
    setCallbacks(found);
    if (found?.backend === 'efax') {
      try { setEfax(await client.getEfaxStatus()); } catch { setEfax(null); }
    } else {
      setEfax(null);
    }
  }, [client, receiving, canReadProviders]);

  const refreshAll = useCallback(() => {
    void fetchList();
    void fetchDeliveries();
    void fetchReceiving();
  }, [fetchList, fetchDeliveries, fetchReceiving]);

  useEffect(() => { void fetchList(); }, [fetchList]);
  useEffect(() => { void fetchDeliveries(); }, [fetchDeliveries]);
  useEffect(() => { void fetchReceiving(); }, [fetchReceiving]);

  useEffect(() => {
    // Received faxes, their owners and their email delivery refresh every 15 seconds.
    const interval = setInterval(() => { void fetchList(); void fetchDeliveries(); }, 15000);
    return () => clearInterval(interval);
  }, [fetchList, fetchDeliveries]);

  const changeFilter = (next: ReceivedFilter) => {
    setFilter(next);
    onShowChange?.(next);
  };

  const deliveryFor = useMemo(() => new Map((deliveries ?? []).filter((item) => item.inbound_fax_id)
    .map((item) => [item.inbound_fax_id as string, item])), [deliveries]);
  const directItems = (deliveries ?? []).filter((item) => item.source === 'direct');

  const rows = useMemo<Row[]>(() => {
    const byFax = new Map((work ?? []).map((item) => [item.inbound_fax_id, item]));
    const faxIds = new Set(faxes.map((fax) => fax.id));
    const all: Row[] = [
      ...faxes.map((fax) => ({ key: fax.id, fax, work: byFax.get(fax.id) ?? null })),
      // Documents this person may work on but not list as faxes still appear.
      ...(work ?? []).filter((item) => !faxIds.has(item.inbound_fax_id)).map((item) => ({ key: item.id, fax: null, work: item })),
    ];
    return all.sort((a, b) => rowTime(b) - rowTime(a));
  }, [faxes, work]);

  const shown = rows.filter((row) => {
    if (filter === 'all') return true;
    if (filter === 'not-delivered') return row.fax !== null && deliveryFor.get(row.fax.id)?.state === 'failed';
    return row.work !== null && viewIds !== null && viewIds.has(row.work.id);
  });

  const filterCount = (value: ReceivedFilter): number | null => {
    if (value === 'mine') return counts?.mine ?? null;
    if (value === 'waiting') return counts?.unassigned ?? null;
    if (value === 'overdue') return counts?.overdue ?? null;
    if (value === 'not-delivered') return notDelivered;
    return null;
  };
  const filterLabel: Record<ReceivedFilter, string> = {
    all: 'All', mine: 'Mine', waiting: 'Waiting for an owner', overdue: 'Overdue', 'not-delivered': 'Not delivered by email',
  };
  const filters = RECEIVED_FILTERS.filter((value) => (value === 'all')
    || (value === 'not-delivered' ? deliveries !== null : work !== null));

  const fetchAgain = async (fax: InboundFax) => {
    setFetchingId(fax.id);
    setError(null);
    setNotice(null);
    try {
      await client.fetchInboundAgain(fax.id);
      setNotice('Faxbot will fetch the document shortly.');
      await fetchList();
    } catch (failure) {
      setError(failure);
    } finally {
      setFetchingId(null);
    }
  };

  const retryDelivery = async (item: IntakeItem) => {
    setRetrying(true);
    setError(null);
    setNotice(null);
    try {
      await client.retryIntakeItem(item.id);
      setNotice('Faxbot will deliver it shortly.');
      await fetchDeliveries();
    } catch (failure) {
      setError(failure);
    } finally {
      setRetrying(false);
    }
  };

  const downloadPdf = async (faxId: string, name: string) => {
    try {
      saveBlob(await client.downloadInboundPdf(faxId), name);
    } catch (failure) {
      setError(failure);
    }
  };

  const act = async (label: string, change: () => Promise<WorkItem>) => {
    setBusy(label);
    try {
      const updated = await change();
      setNotice(workStateSentence(updated));
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(null);
      await fetchList();
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

  const exportEvidence = async (item: WorkItem) => {
    try {
      saveBlob(await client.exportWork(item.id), `evidence-${item.available_at.slice(0, 10)}.zip`);
      setNotice('Evidence downloaded. The export is recorded in the item history.');
    } catch (failure) {
      setError(failure);
    }
  };

  const toneIcon = (tone: DeliveryTone) => {
    if (tone === 'success') return <SuccessIcon />;
    if (tone === 'error') return <ErrorIcon />;
    return <InfoIcon />;
  };

  const formatDate = (value?: string | null) => {
    const date = parseServerTime(value);
    if (!date) return value || '-';
    return isSmallMobile ? date.toLocaleDateString() : date.toLocaleString();
  };

  // When the fax arrived (the provider's time when known), marked when Faxbot brought it in later.
  const arrivedText = (row: Row) => {
    if (!row.fax) return formatDate(row.work?.available_at);
    const value = row.fax.source_received_at || row.fax.received_at;
    const date = parseServerTime(value);
    // Always with the time of day: a recovered fax arrived well before it was brought in.
    const when = date && isSmallMobile
      ? date.toLocaleString(undefined, { month: 'numeric', day: 'numeric', hour: 'numeric', minute: '2-digit' })
      : formatDate(value);
    return row.fax.recovered ? `${when} · brought in later` : when;
  };

  const from = (row: Row) => maskPhoneNumber(row.fax ? row.fax.fr : row.work?.from_number);
  const to = (row: Row) => maskPhoneNumber(row.fax ? row.fax.to : row.work?.to_number);
  const mailbox = (row: Row) => row.work?.mailbox ?? row.fax?.mailbox ?? null;
  const pages = (row: Row) => row.fax?.pages ?? row.work?.pages ?? null;
  const faxId = (row: Row) => row.fax?.id ?? row.work?.inbound_fax_id ?? '';
  const hasDocument = (row: Row) => (row.fax ? inboundFaxStatus(row.fax).hasDocument : true);
  const through = (row: Row) => {
    if (!row.fax) return '-';
    return row.fax.backend === 'import' ? 'Imported' : providerName(row.fax.backend);
  };

  // The fax's own state while its document is missing; otherwise the owner and state of its work item.
  const OwnerAndState = ({ row }: { row: Row }) => {
    const faxStatus = row.fax ? inboundFaxStatus(row.fax) : null;
    if (row.work && (!faxStatus || faxStatus.hasDocument)) {
      return (
        <Box>
          <Chip size="small" color={workColor(row.work)} label={workStateSentence(row.work)}
            sx={{ maxWidth: '100%', height: 'auto', '& .MuiChip-label': { whiteSpace: 'normal', py: 0.25 } }} />
          {(row.work.is_test || row.fax?.is_test) && <Chip size="small" variant="outlined" label="Test fax" sx={{ ml: 1 }} />}
          {duplicateSentence(row.work) && (
            <Typography variant="caption" display="block" sx={{ mt: 0.5 }}>{duplicateSentence(row.work)}</Typography>
          )}
        </Box>
      );
    }
    if (!faxStatus) return <Typography variant="body2">-</Typography>;
    return (
      <Box>
        <Chip icon={toneIcon(faxStatus.tone)} label={faxStatus.label} color={faxStatus.tone} size="small" variant="outlined"
          sx={{ borderRadius: 1 }} />
        {faxStatus.detail && (
          <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5, maxWidth: 280 }}>{faxStatus.detail}</Typography>
        )}
      </Box>
    );
  };

  const Delivery = ({ row }: { row: Row }) => {
    if (!row.fax) return <Typography variant="body2" color="text.secondary">-</Typography>;
    const fax = row.fax;
    return (
      <DeliveryStatusLine item={deliveryFor.get(fax.id)} canRetry={canRetryDelivery} busy={retrying} isNew={isNewFax(fax.received_at)}
        emailApplies={emailDeliveryApplies(connectors, fax.to)} documentPending={!inboundFaxStatus(fax).hasDocument}
        onRetry={(item) => void retryDelivery(item)} label={`the fax from ${maskPhoneNumber(fax.fr)}`} />
    );
  };

  const actions = (row: Row, fullWidth = false) => {
    const item = row.work;
    const fax = row.fax;
    const id = faxId(row);
    return (
      <Stack direction="row" spacing={0.5} flexWrap="wrap" useFlexGap justifyContent={fullWidth ? 'flex-start' : 'flex-end'}
        alignItems="center" onClick={(event) => event.stopPropagation()}>
        <Tooltip title={hasDocument(row) ? 'Download PDF' : 'The document has not arrived yet.'}>
          <span>
            <IconButton size="small" onClick={() => void downloadPdf(id, `received-${(row.fax?.received_at ?? row.work?.available_at ?? '').slice(0, 10) || 'fax'}.pdf`)}
              disabled={!id || !hasDocument(row)} aria-label={`Download the fax from ${from(row)}`}>
              <DownloadIcon />
            </IconButton>
          </span>
        </Tooltip>
        {fax && canChangeProviders && inboundFaxStatus(fax).canFetchAgain && (
          <Button size="small" startIcon={<FetchIcon />} onClick={() => void fetchAgain(fax)} disabled={fetchingId !== null}
            aria-label={`Fetch again the fax from ${maskPhoneNumber(fax.fr)}`}>
            Fetch again
          </Button>
        )}
        {item && can(item, 'assign') && (
          <Button size="small" onClick={(event) => void openAssign(item, event.currentTarget)}>
            {item.owner ? 'Reassign' : 'Assign'}
          </Button>
        )}
        {item && can(item, 'acknowledge') && (
          <Button size="small" variant="contained" disabled={busy !== null}
            onClick={() => void act('acknowledge', () => client.acknowledgeWork(item.id, item.version))}>Acknowledge</Button>
        )}
        {item && can(item, 'done') && (
          <Button size="small" onClick={() => { setNote(''); setFinishing(item); }}>Done</Button>
        )}
        {item && can(item, 'reopen') && (
          <Button size="small" onClick={() => void act('reopen', () => client.reopenWork(item.id, item.version))}>Reopen</Button>
        )}
        {item && can(item, 'export') && (
          <Button size="small" onClick={() => void exportEvidence(item)}>Export</Button>
        )}
      </Stack>
    );
  };

  const sipLine = receiving && callbacks?.backend === 'sip' && (
    <Alert severity={callbacks.receiving?.ready ? 'success' : 'warning'} sx={{ mb: 2, borderRadius: 2 }} data-testid="sip-receiving"
      action={onNavigate && permissions?.has('settings:read') ? (
        <Button color="inherit" size="small" onClick={() => onNavigate('trunk')}>Open trunk settings</Button>
      ) : undefined}>
      {callbacks.receiving?.message ?? 'Receiving over your SIP trunk.'}{' '}
      <Box component="a" href={docsLink('inbound', docsBase)} target="_blank" rel="noreferrer" sx={{ color: 'inherit' }}>How receiving works</Box>
      {canChangeProviders && <InboundRecovery client={client} onRecovered={() => { void fetchList(); }} />}
    </Alert>
  );

  const efaxLine = receiving && callbacks?.backend === 'efax' && efax && (
    <Box sx={{ mb: 2 }}>
      <Alert severity={efax.problem ? 'warning' : 'success'} sx={{ borderRadius: 2 }} data-testid="efax-receiving">
        {efax.receiving ? (efax.problem ?? checkedSentence(efax)) : 'Faxbot is not checking eFax for received faxes.'}
      </Alert>
      {efax.notes.map((line) => <Alert key={line} severity="info" sx={{ mt: 1, borderRadius: 2 }}>{line}</Alert>)}
    </Box>
  );

  return (
    <Box>
      <Box display="flex" justifyContent="space-between" alignItems={{ xs: 'flex-start', sm: 'center' }}
        flexDirection={{ xs: 'column', sm: 'row' }} gap={2} mb={3}>
        <Box>
          <Typography variant="h4" component="h1">Received</Typography>
          <Typography variant="body2" color="text.secondary">
            Faxes that came in to your numbers, who owns each one, and whether it was emailed.
          </Typography>
          {canOpenEmailSettings && (
            <Button variant="text" size="small" onClick={() => onNavigate?.('email')} sx={{ px: 0, minWidth: 0 }}>
              Email delivery settings
            </Button>
          )}
        </Box>
        <Box display="flex" gap={1}>
          {onSendFax && (
            <Button variant="contained" startIcon={<SendIcon />} onClick={onSendFax}
              size={isSmallMobile ? 'medium' : 'large'} sx={{ borderRadius: 2, minHeight: isSmallMobile ? 40 : 42 }}>
              Send a fax
            </Button>
          )}
          <Button variant="outlined" startIcon={<RefreshIcon />} onClick={refreshAll} disabled={loading}
            size={isSmallMobile ? 'medium' : 'large'} sx={{ borderRadius: 2, minHeight: isSmallMobile ? 40 : 42 }}>
            Refresh
          </Button>
        </Box>
      </Box>

      {!receiving && (
        <Alert severity="info" sx={{ mb: 3, borderRadius: 2 }}
          action={onNavigate && permissions?.has('settings:read') && (
            <Button color="inherit" onClick={() => onNavigate('providers/sending')}>Open Sending & receiving</Button>
          )}>
          Receiving faxes is turned off. Turn it on under Providers, Sending & receiving.
        </Alert>
      )}

      {sipLine}
      {efaxLine}

      {loadError && <Alert severity="error" sx={{ mb: 3, borderRadius: 2 }}>{loadError}</Alert>}
      <Notice message={notice} onClose={() => setNotice(null)} />
      <DeliveryError error={error} onClose={() => setError(null)} />

      {(receiving || work !== null) && filters.length > 1 && (
        <ToggleButtonGroup exclusive size="small" value={filter} onChange={(_, next: ReceivedFilter | null) => next && changeFilter(next)}
          aria-label="Which faxes" sx={{ mb: 2, flexWrap: 'wrap' }}>
          {filters.map((value) => {
            const count = filterCount(value);
            return (
              <ToggleButton key={value} value={value} sx={{ textTransform: 'none' }}>
                {filterLabel[value]}{count !== null ? ` (${count})` : ''}
              </ToggleButton>
            );
          })}
        </ToggleButtonGroup>
      )}

      {(receiving || work !== null) && (
        <Box>
          {loading && rows.length === 0 ? (
            <Paper sx={{ p: 4, textAlign: 'center', borderRadius: 2 }}><CircularProgress /></Paper>
          ) : shown.length === 0 ? (
            <Paper sx={{ p: 4, textAlign: 'center', borderRadius: 2 }}>
              <Typography variant="h6" gutterBottom>
                {hasLoaded ? EMPTY[filter] : "Received faxes couldn't be loaded."}
              </Typography>
              <Typography variant="body2" color="text.secondary">
                {hasLoaded ? (filter === 'all' ? 'Received faxes appear here as they arrive.' : 'Choose All to see every received fax.')
                  : 'Select Refresh to try again.'}
              </Typography>
            </Paper>
          ) : isMobile ? (
            <Stack spacing={2}>
              {shown.map((row) => (
                <Card key={row.key} sx={{ borderRadius: 2, cursor: row.work ? 'pointer' : 'default' }}
                  onClick={() => row.work && setSelected(row.work)}>
                  <CardContent>
                    <Stack spacing={1.5}>
                      <Box>
                        <Typography variant="subtitle2" fontWeight={600}>From {from(row)}</Typography>
                        <Typography variant="caption" color="text.secondary">{arrivedText(row)}</Typography>
                      </Box>
                      <Typography variant="body2" color="text.secondary">
                        To {to(row)}{mailbox(row) ? ` · ${mailbox(row)}` : ''} · received through {through(row)}
                        {pages(row) ? ` · ${pages(row)} ${pages(row) === 1 ? 'page' : 'pages'}` : ''}
                      </Typography>
                      {row.fax && <InboundCostLine cost={costs.get(row.fax.id)} />}
                      <OwnerAndState row={row} />
                      {deliveries !== null && row.fax && (
                        <Box>
                          <Typography variant="caption" color="text.secondary" display="block" sx={{ mb: 0.5 }}>Email delivery</Typography>
                          <Delivery row={row} />
                        </Box>
                      )}
                      {actions(row, true)}
                    </Stack>
                  </CardContent>
                </Card>
              ))}
            </Stack>
          ) : (
            <Paper sx={{ borderRadius: 2 }}>
              <TableContainer>
                <Table aria-label="Received faxes">
                  <TableHead>
                    <TableRow>
                      <TableCell>From</TableCell>
                      <TableCell>To</TableCell>
                      <TableCell>Received</TableCell>
                      <TableCell>Received through</TableCell>
                      <TableCell>Pages</TableCell>
                      <TableCell>Owner and status</TableCell>
                      {deliveries !== null && <TableCell>Email delivery</TableCell>}
                      <TableCell align="right">Actions</TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {shown.map((row) => (
                      <TableRow key={row.key} hover onClick={() => row.work && setSelected(row.work)}
                        sx={{ cursor: row.work ? 'pointer' : 'default' }}>
                        <TableCell><Typography variant="body2" fontFamily="monospace">{from(row)}</Typography></TableCell>
                        <TableCell>
                          <Typography variant="body2" fontFamily="monospace">{to(row)}</Typography>
                          {mailbox(row) && <Typography variant="caption" color="text.secondary">{mailbox(row)}</Typography>}
                        </TableCell>
                        <TableCell><Typography variant="caption" color="text.secondary">{arrivedText(row)}</Typography></TableCell>
                        <TableCell>
                          <Typography variant="body2">{through(row)}</Typography>
                          {row.fax && <InboundCostLine cost={costs.get(row.fax.id)} />}
                        </TableCell>
                        <TableCell><Typography variant="body2">{pages(row) || '-'}</Typography></TableCell>
                        <TableCell sx={{ maxWidth: 300 }}><OwnerAndState row={row} /></TableCell>
                        {deliveries !== null && <TableCell><Delivery row={row} /></TableCell>}
                        <TableCell align="right">{actions(row)}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </TableContainer>
              <Box sx={{ p: 2, borderTop: '1px solid', borderColor: 'divider' }}>
                <Typography variant="caption" color="text.secondary">
                  Updates every 15 seconds. Phone numbers show only their last four digits.
                </Typography>
              </Box>
            </Paper>
          )}
        </Box>
      )}

      {receiving && (
        <DirectDeliveries items={directItems} canRetry={canRetryDelivery} busy={retrying} onRetry={(item) => void retryDelivery(item)} />
      )}

      {canWork && permissions?.has('settings:read') && (
        <Box sx={{ mt: 4 }}>
          <WorkSettingsPanel client={client} canWrite={permissions.has('settings:write')} />
        </Box>
      )}

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

      <WorkDetail client={client} item={selected} onClose={() => setSelected(null)}
        onDownload={(item) => void downloadPdf(item.inbound_fax_id, `document-${item.available_at.slice(0, 10)}.pdf`)} />
    </Box>
  );
}
