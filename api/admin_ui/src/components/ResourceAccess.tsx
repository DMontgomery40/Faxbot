import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Box,
  Fade,
  Button,
  Checkbox,
  FormControlLabel,
  IconButton,
  Paper,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Tooltip,
  Typography,
} from '@mui/material';
import AddIcon from '@mui/icons-material/Add';
import DeleteIcon from '@mui/icons-material/Delete';
import EditIcon from '@mui/icons-material/Edit';
import BlockIcon from '@mui/icons-material/Block';
import CheckIcon from '@mui/icons-material/CheckCircleOutline';
import LockOpenIcon from '@mui/icons-material/LockOpen';
import MailIcon from '@mui/icons-material/MarkunreadMailbox';
import PhoneIcon from '@mui/icons-material/Phone';
import AdminAPIClient, { plainRefusal } from '../api/client';
import type {
  AccessAssignment,
  AccessGroup,
  AccessMailbox,
  AccessResource,
  AccessRole,
  AccessUser,
  AuthMe,
  InboundRule,
} from '../api/types';
import {
  ConfirmDialog,
  EmptyState,
  ErrorBanner,
  Field,
  FormDialog,
  LoadStateView,
  ScreenHeader,
  SelectField,
  StatusChip,
  loadFailure,
  type LoadState,
} from './access/AccessViews';
import { resourceLabel } from './access/permissions';
import { numberHint, numberPlaceholder, useNumberFormat } from './common/numbers';
import type { EmailConnector } from '../api/deliveryTypes';
import type { Settings } from '../api/types';
import type { AdminDestination } from '../navigation';
import { providerLabel } from '../providerLabels';
import { NO_RECEIVING_OPTIONS, rulesApiFor, type ReceivingOptions } from './ProviderRulesApi';
import { ReceivedTry, ReceivingOptionsFields, type Named } from './ProviderRulesReceiving';
import { receivingSentence } from './ProviderRulesText';

const OPTION_KEYS = Object.keys(NO_RECEIVING_OPTIONS) as Array<keyof ReceivingOptions>;

// A number rule's receiving options, with today's behaviour for any the server does not send.
function optionsOf(rule: InboundRule): ReceivingOptions {
  const options = { ...NO_RECEIVING_OPTIONS } as Record<string, unknown>;
  for (const key of OPTION_KEYS) if (rule[key] !== undefined && rule[key] !== null) options[key] = rule[key];
  return options as unknown as ReceivingOptions;
}

function hasOptions(rule: InboundRule): boolean {
  return Object.keys(changedOptions(NO_RECEIVING_OPTIONS, optionsOf(rule))).filter((key) => key !== 'position').length > 0;
}

function changedOptions(before: ReceivingOptions, after: ReceivingOptions): Partial<ReceivingOptions> {
  const changed: Record<string, unknown> = {};
  for (const key of OPTION_KEYS) {
    if (JSON.stringify(before[key]) !== JSON.stringify(after[key])) changed[key] = after[key];
  }
  return changed as Partial<ReceivingOptions>;
}

// Who has access is under Access; mailboxes and fax numbers are under Numbers.
export type ResourceAccessSection = 'assignments' | 'mailboxes' | 'numbers';

export const INSTALLATION_WARNING = 'Applies to every fax and mailbox, including history';

// Shared reload: refresh the policy version, then the section's data.
function useSection<T>(client: AdminAPIClient, fetcher: () => Promise<T>) {
  const [data, setData] = useState<T | null>(null);
  const [state, setState] = useState<LoadState>('loading');
  const load = useCallback(async () => {
    setState((current) => (current === 'ready' ? current : 'loading'));
    try {
      setData(await fetcher());
      setState('ready');
    } catch (failure) {
      setState(loadFailure(failure));
    }
  }, [fetcher]);
  useEffect(() => { void load(); }, [load]);
  const reload = useCallback(async () => {
    await client.me().catch(() => undefined);
    await load();
  }, [client, load]);
  return { data, state, load, reload };
}

function AssignmentsSection({ client, canManage }: { client: AdminAPIClient; canManage: boolean }) {
  const fetcher = useCallback(async () => {
    const [assignments, users, groups, roles, resources] = await Promise.all([
      client.listAssignments(),
      client.listUsers({ kind: 'all', limit: 200 }).catch(() => ({ items: [] as AccessUser[] })),
      client.listGroups().catch(() => ({ items: [] as AccessGroup[] })),
      client.listRoles().catch(() => ({ items: [] as AccessRole[] })),
      client.listResources().catch(() => ({ items: [] as AccessResource[] })),
    ]);
    return { assignments: assignments.items, users: users.items, groups: groups.items, roles: roles.items, resources: resources.items };
  }, [client]);
  const { data, state, load, reload } = useSection(client, fetcher);

  const [draft, setDraft] = useState<{ subject: string; roleId: string; resourceId: string } | null>(null);
  const [removing, setRemoving] = useState<AccessAssignment | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  const subjects = useMemo(() => [
    ...(data?.groups ?? []).filter((g) => g.enabled).map((g) => ({ value: `group:${g.id}`, label: `Group: ${g.name}` })),
    ...(data?.users ?? []).filter((u) => u.enabled && u.kind !== 'bootstrap').map((u) => ({ value: `principal:${u.id}`, label: u.display_name })),
  ], [data]);
  const resource = data?.resources.find((r) => r.id === draft?.resourceId);

  const submit = async () => {
    if (!draft || !data) return;
    const [kind, subjectId] = draft.subject.split(':') as ['principal' | 'group', string];
    const version = kind === 'group' ? data.groups.find((g) => g.id === subjectId)?.version : data.users.find((u) => u.id === subjectId)?.version;
    const role = data.roles.find((r) => r.id === draft.roleId);
    if (version === undefined || !role) return;
    setBusy(true);
    setError(null);
    try {
      await client.createAssignment({ subject: { kind, id: subjectId, version }, role: { id: role.id, version: role.version }, resource_id: draft.resourceId });
      setDraft(null);
      await load();
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!removing) return;
    setBusy(true);
    setError(null);
    try {
      await client.removeAssignment(removing.id, removing.version);
      setRemoving(null);
      await load();
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };

  const reloadAll = async () => { setError(null); await reload(); };

  return (
    <Box>
      <ScreenHeader title="Who has access" subtitle="Give a person or group a role on everything, a mailbox, or someone's own faxes."
        onRefresh={() => void reloadAll()} busy={state === 'loading'}>
        {canManage && (
          <Button variant="contained" startIcon={<AddIcon />} disabled={state !== 'ready'}
            onClick={() => { setError(null); setDraft({ subject: '', roleId: '', resourceId: '' }); }}>
            Give access
          </Button>
        )}
      </ScreenHeader>
      {!draft && !removing && <ErrorBanner error={error} onReload={() => void reloadAll()} onClose={() => setError(null)} />}
      {state !== 'ready' || !data ? <LoadStateView state={state} onRetry={() => void load()} />
        : data.assignments.length === 0 ? (
          <EmptyState icon={<LockOpenIcon />} title="No access given yet" text="People and groups see nothing until they are given a role here." />
        ) : (
          <TableContainer component={Paper} sx={{ borderRadius: 2 }}>
            <Table>
              <TableHead>
                <TableRow>
                  <TableCell>Who</TableCell>
                  <TableCell>Role</TableCell>
                  <TableCell>Where</TableCell>
                  {canManage && <TableCell align="right">Actions</TableCell>}
                </TableRow>
              </TableHead>
              <TableBody>
                {data.assignments.map((a) => (
                  <TableRow key={a.id} hover>
                    <TableCell>
                      <Typography variant="body2" fontWeight={600}>{a.subject.name}</Typography>
                      <Typography variant="caption" color="text.secondary">{a.subject.kind === 'group' ? 'Group' : 'Person or integration'}</Typography>
                    </TableCell>
                    <TableCell>{a.role.name}</TableCell>
                    <TableCell>{resourceLabel(a.resource)}</TableCell>
                    {canManage && (
                      <TableCell align="right">
                        <Tooltip title="Remove">
                          <IconButton aria-label={`Remove ${a.role.name} from ${a.subject.name}`} size="small" color="error"
                            onClick={() => { setError(null); setRemoving(a); }}>
                            <DeleteIcon />
                          </IconButton>
                        </Tooltip>
                      </TableCell>
                    )}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableContainer>
        )}

      <FormDialog client={client} open={draft !== null} title="Give access" submitLabel="Give access" busy={busy} error={error}
        canSubmit={Boolean(draft?.subject && draft.roleId && draft.resourceId)} onSubmit={() => void submit()}
        onClose={() => setDraft(null)} onReload={() => void reloadAll()}>
        {draft && data && (
          <>
            <SelectField label="Who" value={draft.subject} onChange={(subject) => setDraft({ ...draft, subject })} options={subjects} />
            <SelectField label="Role" value={draft.roleId} onChange={(roleId) => setDraft({ ...draft, roleId })}
              options={data.roles.filter((r) => r.enabled).map((r) => ({ value: r.id, label: r.name }))} />
            <SelectField label="Where" value={draft.resourceId} onChange={(resourceId) => setDraft({ ...draft, resourceId })}
              options={data.resources.map((r) => ({ value: r.id, label: resourceLabel(r) }))} />
            {resource?.kind === 'installation' && <Alert severity="warning" sx={{ mt: 1, borderRadius: 2 }}>{INSTALLATION_WARNING}</Alert>}
          </>
        )}
      </FormDialog>

      <ConfirmDialog client={client} open={removing !== null} title="Remove access?"
        text={removing ? `${removing.subject.name} loses ${removing.role.name} on ${resourceLabel(removing.resource).toLowerCase()}.` : ''}
        confirmLabel="Remove" danger busy={busy} error={error} onConfirm={() => void remove()} onCancel={() => setRemoving(null)} onReload={() => void reloadAll()} />
    </Box>
  );
}

function MailboxesSection({ client, canManage }: { client: AdminAPIClient; canManage: boolean }) {
  const fetcher = useCallback(async () => (await client.listMailboxes()).items, [client]);
  const { data, state, load, reload } = useSection<AccessMailbox[]>(client, fetcher);
  const [draft, setDraft] = useState<{ mailboxId: string | null; label: string; enabled: boolean } | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  const save = async (change?: { mailbox: AccessMailbox; enabled: boolean }) => {
    setBusy(true);
    setError(null);
    try {
      if (change) {
        // A one-click change starts now; act on the current access policy.
        await client.refreshPolicy();
        await client.updateMailbox(change.mailbox.id, { enabled: change.enabled, version: change.mailbox.version });
      } else if (draft?.mailboxId) {
        const mailbox = data?.find((m) => m.id === draft.mailboxId);
        if (!mailbox) return;
        await client.updateMailbox(mailbox.id, { label: draft.label.trim(), version: mailbox.version });
        setDraft(null);
      } else if (draft) {
        await client.createMailbox({ label: draft.label.trim(), enabled: draft.enabled });
        setDraft(null);
      }
      await load();
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };

  const reloadAll = async () => { setError(null); await reload(); };

  return (
    <Box>
      <ScreenHeader title="Mailboxes" subtitle="Received faxes land in a mailbox. Give people access to the mailboxes they need."
        onRefresh={() => void reloadAll()} busy={state === 'loading'}>
        {canManage && (
          <Button variant="contained" startIcon={<AddIcon />} disabled={state !== 'ready'}
            onClick={() => { setError(null); setDraft({ mailboxId: null, label: '', enabled: true }); }}>
            Add mailbox
          </Button>
        )}
      </ScreenHeader>
      {!draft && <ErrorBanner error={error} onReload={() => void reloadAll()} onClose={() => setError(null)} />}
      {state !== 'ready' || !data ? <LoadStateView state={state} onRetry={() => void load()} />
        : data.length === 0 ? <EmptyState icon={<MailIcon />} title="No mailboxes" text="Add a mailbox for each team or line that receives faxes." />
        : (
          <TableContainer component={Paper} sx={{ borderRadius: 2 }}>
            <Table>
              <TableHead>
                <TableRow>
                  <TableCell>Name</TableCell>
                  <TableCell>Status</TableCell>
                  <TableCell>Fax numbers</TableCell>
                  {canManage && <TableCell align="right">Actions</TableCell>}
                </TableRow>
              </TableHead>
              <TableBody>
                {data.map((m) => (
                  <TableRow key={m.id} hover>
                    <TableCell><Typography variant="body2" fontWeight={600}>{m.label}</Typography></TableCell>
                    <TableCell><StatusChip label={m.enabled ? 'Active' : 'Disabled'} tone={m.enabled ? 'success' : 'default'} /></TableCell>
                    <TableCell>{m.rule_count}</TableCell>
                    {canManage && (
                      <TableCell align="right" sx={{ whiteSpace: 'nowrap' }}>
                        <Tooltip title="Rename">
                          <IconButton aria-label={`Rename ${m.label}`} size="small"
                            onClick={() => { setError(null); setDraft({ mailboxId: m.id, label: m.label, enabled: m.enabled }); }}>
                            <EditIcon />
                          </IconButton>
                        </Tooltip>
                        <Tooltip title={m.enabled ? 'Disable' : 'Enable'}>
                          <IconButton aria-label={`${m.enabled ? 'Disable' : 'Enable'} ${m.label}`} size="small" disabled={busy}
                            onClick={() => void save({ mailbox: m, enabled: !m.enabled })}>
                            {m.enabled ? <BlockIcon /> : <CheckIcon />}
                          </IconButton>
                        </Tooltip>
                      </TableCell>
                    )}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableContainer>
        )}
      <FormDialog client={client} open={draft !== null} title={draft?.mailboxId ? 'Rename mailbox' : 'Add mailbox'}
        submitLabel={draft?.mailboxId ? 'Save' : 'Add mailbox'} busy={busy} error={error}
        canSubmit={Boolean(draft?.label.trim())} onSubmit={() => void save()} onClose={() => setDraft(null)} onReload={() => void reloadAll()}>
        {draft && (
          <>
            <Field label="Mailbox name" value={draft.label} onChange={(label) => setDraft({ ...draft, label })} autoFocus
              helperText={draft.mailboxId ? 'Access and fax numbers stay with the mailbox when you rename it.' : undefined} />
            {!draft.mailboxId && (
              <FormControlLabel label="Enabled" control={<Checkbox checked={draft.enabled} onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })} />} />
            )}
          </>
        )}
      </FormDialog>
    </Box>
  );
}

// A fax number in one form for comparing: the international form where it can be
// told, so a HumbleFax number typed as 10 digits matches its +1 mailbox rule.
export function comparableNumber(value: string | null | undefined): string {
  const text = (value ?? '').trim();
  const digits = text.replace(/\D/g, '');
  if (!digits) return '';
  if (text.startsWith('+')) return `+${digits}`;
  if (digits.length === 11 && digits.startsWith('1')) return `+${digits}`;
  if (digits.length === 10) return `+1${digits}`;
  return digits;
}

// The numbers each provider this installation knows carries, from the settings document.
export interface CarriedNumber {
  number: string;
  provider: string;
  label: string;
  page: AdminDestination;
  inUse: boolean;
}

const CARRIER_PAGES: Record<string, { label: string; page: AdminDestination }> = {
  sip: { label: 'Carrier trunk', page: 'providers/trunk' },
  humblefax: { label: 'HumbleFax', page: 'providers/humblefax' },
  efax: { label: 'eFax', page: 'providers/efax' },
  signalwire: { label: 'SignalWire', page: 'providers/signalwire' },
};

export function carriedNumbers(settings: Settings): CarriedNumber[] {
  const sending = settings.hybrid?.outbound_backend ?? settings.backend.type;
  const receiving = settings.inbound.enabled ? (settings.hybrid?.inbound_backend ?? settings.backend.type) : '';
  const extra = (settings.routing?.outbound_routes ?? '').split(',').map((route) => route.trim());
  const used = (provider: string) => provider === sending || provider === receiving || extra.includes(provider);
  const trunk = (settings.sip as { trunk?: { dids?: string[] } }).trunk;
  const found: Array<[string, string | null | undefined]> = [
    ...(trunk?.dids ?? []).map((number): [string, string] => ['sip', number]),
    ['humblefax', settings.humblefax?.from_number],
    // Every number on the HumbleFax account, as HumbleFax lists them.
    ...(settings.humblefax?.account_numbers ?? []).map((number): [string, string] => ['humblefax', number]),
    ['efax', settings.efax?.caller_id],
    ['signalwire', settings.signalwire?.from_fax],
  ];
  const seen = new Set<string>();
  return found.filter(([provider, number]) => {
    const key = `${provider} ${comparableNumber(number)}`;
    if (!comparableNumber(number) || seen.has(key)) return false;
    seen.add(key);
    return true;
  }).map(([provider, number]) => ({
    number: comparableNumber(number), provider,
    // The trunk is named by its carrier or phone system once one is chosen.
    label: provider === 'sip' ? providerLabel('sip') : CARRIER_PAGES[provider].label, page: CARRIER_PAGES[provider].page,
    inUse: used(provider),
  }));
}

export const NO_MAILBOX = 'No mailbox: received faxes are visible to people with access to everything.';

interface NumberRow { number: string; rule: InboundRule | null; carriers: CarriedNumber[] }

function NumbersSection({ client, canManage, canReadSettings, onNavigate }: {
  client: AdminAPIClient;
  canManage: boolean;
  // Carried numbers come from the settings document, read with settings:read.
  canReadSettings: boolean;
  onNavigate?: (destination: AdminDestination) => void;
}) {
  const fetcher = useCallback(async () => {
    const [rules, mailboxes] = await Promise.all([client.listInboundRules(), client.listMailboxes()]);
    return { rules: rules.items, mailboxes: mailboxes.items };
  }, [client]);
  const { data, state, load, reload } = useSection<{ rules: InboundRule[]; mailboxes: AccessMailbox[] }>(client, fetcher);
  const [carried, setCarried] = useState<CarriedNumber[]>([]);
  const [connectors, setConnectors] = useState<EmailConnector[] | null>(null);
  const [draft, setDraft] = useState<{ ruleId: string | null; toNumber: string; mailboxId: string; options: ReceivingOptions } | null>(null);
  const [showOptions, setShowOptions] = useState(false);
  // Accounts that receive faxes and the installation's time zone, for the receiving options.
  const [receivingAccounts, setReceivingAccounts] = useState<Named[]>([]);
  const [sites, setSites] = useState<Named[]>([]);
  const [timeZone, setTimeZone] = useState("Faxbot's time zone");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState<string | null>(null);
  const numberFormat = useNumberFormat(client);

  const loadExtras = useCallback(async () => {
    const [settings, email] = await Promise.allSettled([
      canReadSettings ? client.getSettings() : Promise.resolve(null),
      client.listEmailConnectors(),
    ]);
    setCarried(settings.status === 'fulfilled' && settings.value ? carriedNumbers(settings.value) : []);
    setConnectors(email.status === 'fulfilled' ? email.value.connectors : null);
    const zone = settings.status === 'fulfilled' ? settings.value?.installation?.time_zone : undefined;
    if (zone) setTimeZone(zone);
    if (canReadSettings) {
      rulesApiFor(client).accounts()
        .then((accounts) => {
          setReceivingAccounts(accounts.accounts.filter((account) => account.receives)
            .map((account) => ({ key: account.key, label: account.label })));
          setSites(accounts.sites.map((site) => ({ key: site.key, label: site.name })));
        })
        .catch(() => setReceivingAccounts([]));
    }
  }, [client, canReadSettings]);
  useEffect(() => { void loadExtras(); }, [loadExtras]);

  const rows = useMemo<NumberRow[]>(() => {
    const byNumber = new Map<string, NumberRow>();
    for (const rule of data?.rules ?? []) {
      byNumber.set(comparableNumber(rule.to_number) || rule.to_number, { number: rule.to_number, rule, carriers: [] });
    }
    for (const entry of carried) {
      const row = byNumber.get(entry.number) ?? { number: entry.number, rule: null, carriers: [] };
      row.carriers.push(entry);
      byNumber.set(entry.number, row);
    }
    return [...byNumber.values()];
  }, [data, carried]);

  const emailText = (number: string) => {
    if (connectors === null) return '-';
    const covering = connectors.filter((connector) => connector.enabled
      && (connector.match_number === null || comparableNumber(connector.match_number) === comparableNumber(number)));
    if (covering.length === 0) return 'Not emailed';
    const recipients = [...new Set(covering.flatMap((connector) => connector.recipients))];
    return recipients.length ? `Emailed to ${recipients.join(', ')}` : 'Emailed';
  };

  const save = async () => {
    if (!draft || !data) return;
    setBusy(true);
    setError(null);
    setSaved(null);
    try {
      const rule = draft.ruleId ? data.rules.find((r) => r.id === draft.ruleId) : undefined;
      // Only the receiving options that changed are sent, so a number rule without options stays as it was.
      const changes = changedOptions(rule ? optionsOf(rule) : NO_RECEIVING_OPTIONS, draft.options);
      // The number is sent as typed; the server saves it in international form.
      const result = rule
        ? await client.updateInboundRule(rule.id, {
          ...(draft.toNumber.trim() !== rule.to_number ? { to_number: draft.toNumber.trim() } : {}),
          ...(draft.mailboxId !== rule.mailbox_id ? { mailbox_id: draft.mailboxId } : {}),
          ...changes,
          version: rule.version,
        })
        : await client.createInboundRule({ to_number: draft.toNumber.trim(), mailbox_id: draft.mailboxId, ...changes });
      const mailbox = data.mailboxes.find((m) => m.id === draft.mailboxId)?.label;
      const number = result?.rule?.to_number;
      setDraft(null);
      if (number) setSaved(mailbox ? `Faxes to ${number} now go to ${mailbox}.` : `Saved ${number}.`);
      await load();
    } catch (failure) {
      const sentence = plainRefusal(failure);
      setError(sentence ? new Error(sentence) : failure);
    } finally {
      setBusy(false);
    }
  };

  const reloadAll = async () => { setError(null); await reload(); await loadExtras(); };
  const choose = (row: NumberRow) => {
    setError(null);
    setSaved(null);
    setShowOptions(Boolean(row.rule && hasOptions(row.rule)));
    setDraft(row.rule ? { ruleId: row.rule.id, toNumber: row.rule.to_number, mailboxId: row.rule.mailbox_id, options: optionsOf(row.rule) }
      : { ruleId: null, toNumber: row.number, mailboxId: '', options: NO_RECEIVING_OPTIONS });
  };

  return (
    <Box>
      <ScreenHeader title="Your numbers"
        subtitle="Each fax number Faxbot carries: who carries it, which mailbox receives its faxes, whether they are emailed and who can see them."
        onRefresh={() => void reloadAll()} busy={state === 'loading'}>
        {canManage && (
          <Button variant="contained" startIcon={<AddIcon />} disabled={state !== 'ready' || !data?.mailboxes.length}
            onClick={() => { setError(null); setSaved(null); setShowOptions(false);
              setDraft({ ruleId: null, toNumber: '', mailboxId: '', options: NO_RECEIVING_OPTIONS }); }}>
            Add number
          </Button>
        )}
      </ScreenHeader>
      {!draft && <ErrorBanner error={error} onReload={() => void reloadAll()} onClose={() => setError(null)} />}
      {saved && (
        <Fade in>
          <Alert severity="success" sx={{ mb: 3, borderRadius: 2 }} onClose={() => setSaved(null)}>{saved}</Alert>
        </Fade>
      )}
      {state !== 'ready' || !data ? <LoadStateView state={state} onRetry={() => void load()} />
        : rows.length === 0 ? <EmptyState icon={<PhoneIcon />} title="No fax numbers yet"
            text={data.mailboxes.length ? 'Add a number to send its faxes to a mailbox.' : 'Add a mailbox first, then route numbers to it.'} />
        : (
          <TableContainer component={Paper} sx={{ borderRadius: 2 }}>
            <Table aria-label="Your numbers">
              <TableHead>
                <TableRow>
                  <TableCell>Fax number</TableCell>
                  <TableCell>Provided by</TableCell>
                  <TableCell>Mailbox</TableCell>
                  <TableCell>Email delivery</TableCell>
                  <TableCell>Who can see its faxes</TableCell>
                  {canManage && <TableCell align="right">Actions</TableCell>}
                </TableRow>
              </TableHead>
              <TableBody>
                {rows.map((row) => (
                  <TableRow key={row.number} hover>
                    <TableCell>{row.number}</TableCell>
                    <TableCell>
                      {row.carriers.length === 0 ? <Typography variant="body2" color="text.secondary">-</Typography>
                        : row.carriers.map((entry) => (
                          <Box key={entry.provider}>
                            {onNavigate ? (
                              <Button size="small" sx={{ px: 0, minWidth: 0, textTransform: 'none' }} onClick={() => onNavigate(entry.page)}>
                                {entry.label}
                              </Button>
                            ) : <Typography variant="body2">{entry.label}</Typography>}
                            {!entry.inUse && <Typography variant="caption" color="text.secondary" display="block">Not in use now</Typography>}
                          </Box>
                        ))}
                    </TableCell>
                    <TableCell>
                      {row.rule ? row.rule.mailbox_label
                        : <Typography variant="body2" color="text.secondary" sx={{ maxWidth: 260 }}>{NO_MAILBOX}</Typography>}
                      {row.rule && hasOptions(row.rule) && (
                        <Typography variant="caption" color="text.secondary" display="block" sx={{ maxWidth: 360 }}>
                          {row.rule.enabled === false ? 'Off: ' : ''}{receivingSentence({ ...row.rule }, {
                            account: (key) => receivingAccounts.find((account) => account.key === key)?.label ?? key,
                            site: (key) => sites.find((site) => site.key === key)?.label ?? key,
                            connector: (id) => connectors?.find((connector) => connector.id === id)?.name ?? 'another email connector',
                          })}
                        </Typography>
                      )}
                    </TableCell>
                    <TableCell>{emailText(row.number)}</TableCell>
                    <TableCell>
                      {row.rule ? `People with access to ${row.rule.mailbox_label}, or to everything` : 'People with access to everything'}
                    </TableCell>
                    {canManage && (
                      <TableCell align="right">
                        {row.rule ? (
                          <Tooltip title="Edit">
                            <IconButton aria-label={`Edit ${row.rule.to_number}`} size="small" onClick={() => choose(row)}>
                              <EditIcon />
                            </IconButton>
                          </Tooltip>
                        ) : (
                          <Button size="small" disabled={!data.mailboxes.length} onClick={() => choose(row)}
                            aria-label={`Choose a mailbox for ${row.number}`}>
                            Choose mailbox
                          </Button>
                        )}
                      </TableCell>
                    )}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableContainer>
        )}
      {state === 'ready' && rows.length > 0 && (
        <Box sx={{ mt: 3 }}>
          <ReceivedTry api={rulesApiFor(client)} accounts={receivingAccounts} timeZone={timeZone} />
        </Box>
      )}
      {!canReadSettings && state === 'ready' && (
        <Typography variant="caption" color="text.secondary" display="block" sx={{ mt: 1 }}>
          Numbers without a mailbox are shown only to people who can see settings.
        </Typography>
      )}
      <FormDialog client={client} open={draft !== null} title={draft?.ruleId ? 'Edit fax number' : 'Add fax number'}
        submitLabel={draft?.ruleId ? 'Save' : 'Add number'} busy={busy} error={error}
        canSubmit={Boolean(draft?.toNumber.trim() && draft.mailboxId)} onSubmit={() => void save()} onClose={() => setDraft(null)}
        onReload={() => void reloadAll()}>
        {draft && data && (
          <>
            <Field label="Fax number" value={draft.toNumber} onChange={(toNumber) => setDraft({ ...draft, toNumber })} autoFocus
              type="tel" placeholder={numberPlaceholder(numberFormat)} helperText={numberHint(numberFormat, 'The number faxes are sent to')} />
            <SelectField label="Mailbox" value={draft.mailboxId} onChange={(mailboxId) => setDraft({ ...draft, mailboxId })}
              options={data.mailboxes.map((m) => ({ value: m.id, label: m.label }))} />
            <Button size="small" sx={{ alignSelf: 'flex-start', mt: 1 }} onClick={() => setShowOptions(!showOptions)}>
              {showOptions ? 'Fewer choices' : 'More choices: account, sender, times, email, urgency'}
            </Button>
            {showOptions && (
              <Box sx={{ mt: 1 }}>
                <ReceivingOptionsFields value={draft.options} onChange={(options) => setDraft({ ...draft, options })}
                  accounts={receivingAccounts} timeZone={timeZone} sites={sites}
                  connectors={(connectors ?? []).map((connector) => ({ key: connector.id, label: connector.name }))} />
              </Box>
            )}
          </>
        )}
      </FormDialog>
    </Box>
  );
}

export default function ResourceAccess({ client, me, section, onNavigate }: {
  client: AdminAPIClient;
  me: AuthMe;
  section: ResourceAccessSection;
  onNavigate?: (destination: AdminDestination) => void;
}) {
  const permissions = useMemo(() => new Set(me.permissions), [me.permissions]);
  const allowed = section === 'assignments'
    ? permissions.has('grants:read') || permissions.has('grants:manage')
    : permissions.has('mailboxes:read') || permissions.has('mailboxes:manage');

  if (!allowed) return <LoadStateView state="denied" />;

  return (
    <Box>
      {section === 'assignments' && <AssignmentsSection client={client} canManage={permissions.has('grants:manage')} />}
      {section === 'mailboxes' && <MailboxesSection client={client} canManage={permissions.has('mailboxes:manage')} />}
      {section === 'numbers' && <NumbersSection client={client} canManage={permissions.has('mailboxes:manage')}
        canReadSettings={permissions.has('settings:read')} onNavigate={onNavigate} />}
    </Box>
  );
}
