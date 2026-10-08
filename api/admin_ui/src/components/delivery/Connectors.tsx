// Numbers → Email and folders: mailboxes and folders that bring documents into Faxbot or send faxes.
// Each document is filed or faxed once; a copy seen again is counted, never handled twice.
import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert, Box, Button, Card, CardContent, Chip, IconButton, Stack, Table, TableBody, TableCell, TableHead, TableRow,
  TextField, Typography,
} from '@mui/material';
import AllInboxIcon from '@mui/icons-material/AllInbox';
import DeleteIcon from '@mui/icons-material/Delete';
import AdminAPIClient, { AdminAPIError, isForbidden } from '../../api/client';
import type {
  Connector, ConnectorChoices, ConnectorDirection, ConnectorItem, ConnectorKind, ConnectorSettings,
} from '../../api/connectorTypes';
import { formatServerTime } from '../../api/time';
import SecretInput from '../common/SecretInput';
import { ConfirmDialog, EmptyState, Field, FormDialog } from '../access/AccessViews';
import { DeliveryError, Notice } from './shared';

type Purpose = 'mail-in' | 'folder-in' | 'mail-out' | 'folder-out';

const PURPOSES: { id: Purpose; label: string; kind: ConnectorKind; direction: ConnectorDirection }[] = [
  { id: 'mail-in', label: 'Bring in documents from a mailbox', kind: 'email', direction: 'receive' },
  { id: 'folder-in', label: 'Bring in documents from a folder', kind: 'folder', direction: 'receive' },
  { id: 'mail-out', label: 'Fax what people email', kind: 'email', direction: 'send' },
  { id: 'folder-out', label: 'Fax files put in a folder', kind: 'folder', direction: 'send' },
];

const SERVICES: { id: string; label: string }[] = [
  { id: 'microsoft365', label: 'Microsoft 365' },
  { id: 'google', label: 'Google Workspace' },
  { id: 'other', label: 'Another mail service' },
];

const SIGN_INS: { id: string; label: string }[] = [
  { id: 'microsoft_app', label: 'Microsoft 365 app (tenant ID, client ID and client secret)' },
  { id: 'google_service_account', label: 'Google service account key' },
  { id: 'password', label: 'Password or app password' },
  { id: 'oauth_refresh', label: 'Another service\'s sign-in token' },
];

interface SenderRow { address: string; principal_id: string }

interface Draft {
  name: string;
  purpose: Purpose;
  settings: ConnectorSettings;
  secret: Record<string, string>;
  senders: SenderRow[];
}

function purposeOf(connector: Connector): Purpose {
  return PURPOSES.find((item) => item.kind === connector.kind && item.direction === connector.direction)?.id ?? 'mail-in';
}

function Select({ label, value, onChange, options, helperText }: {
  label: string; value: string; onChange: (value: string) => void; options: { id: string; label: string }[];
  helperText?: string;
}) {
  return (
    <TextField select fullWidth margin="normal" label={label} value={value} helperText={helperText}
      onChange={(event) => onChange(event.target.value)} SelectProps={{ native: true }} InputLabelProps={{ shrink: true }}>
      {options.map((option) => <option key={option.id} value={option.id}>{option.label}</option>)}
    </TextField>
  );
}

// A refusal the server explains in a sentence (a mailbox you can't see, a key you may not manage) is shown as said.
function refusalSentence(error: unknown): string | null {
  return error instanceof AdminAPIError && error.status === 403 && typeof error.detail === 'string'
    && error.detail !== 'This operation is not permitted.' && /^[A-Z][^<>{}]{3,240}[.!?]$/.test(error.detail)
    ? error.detail : null;
}

function ConnectorDialog({ connector, choices, busy, error, onSave, onClose }: {
  connector: Connector | 'new';
  choices: ConnectorChoices | null;
  busy: boolean;
  error: unknown;
  onSave: (draft: Draft) => void;
  onClose: () => void;
}) {
  const existing = connector === 'new' ? null : connector;
  const [draft, setDraft] = useState<Draft>(() => ({
    name: existing?.name ?? '',
    purpose: existing ? purposeOf(existing) : 'mail-in',
    settings: existing ? { ...existing.settings } : { provider: 'microsoft365', sign_in: 'microsoft_app' },
    secret: {},
    senders: existing?.senders?.map(({ address, principal_id }) => ({ address, principal_id })) ?? [],
  }));
  const purpose = PURPOSES.find((item) => item.id === draft.purpose) ?? PURPOSES[0];
  const value = (key: string) => (draft.settings[key] ?? '') as string;
  const set = (key: string, next: string | number | null) =>
    setDraft((current) => ({ ...current, settings: { ...current.settings, [key]: next } }));
  const setSecret = (key: string, next: string) =>
    setDraft((current) => ({ ...current, secret: { ...current.secret, [key]: next } }));
  const provider = choices?.providers.find((item) => item.id === (value('provider') || 'other'));
  const signIn = value('sign_in') || provider?.sign_in || 'password';
  const keep = existing?.has_secret ? 'Leave empty to keep the saved one.' : undefined;
  const chooseService = (next: string) => {
    const preset = choices?.providers.find((item) => item.id === next);
    setDraft((current) => ({ ...current, settings: { ...current.settings, provider: next,
      sign_in: preset?.sign_in ?? 'password', checked_by: null } }));
  };
  const number = (key: string) => (text: string) => set(key, text === '' ? null : Number(text) || 0);
  const ready = Boolean(draft.name.trim()) && (purpose.kind === 'folder' ? Boolean(value('path')) : Boolean(value('address')));
  return (
    <FormDialog open title={existing ? `Change ${existing.name}` : 'Add a connector'} submitLabel="Save" busy={busy}
      error={null} canSubmit={ready} onSubmit={() => onSave({ ...draft, settings: { ...draft.settings, sign_in: purpose.kind === 'email' ? signIn : null } })}
      onClose={onClose}>
      {refusalSentence(error)
        ? <Alert severity="error" sx={{ mb: 3, borderRadius: 2 }}>{refusalSentence(error)}</Alert>
        : <DeliveryError error={error} />}
      <Field label="Name" value={draft.name} onChange={(next) => setDraft((current) => ({ ...current, name: next }))}
        helperText="For example, Scanner share or Email to fax." />
      {!existing && (
        <Select label="What it does" value={draft.purpose} options={PURPOSES}
          onChange={(next) => setDraft((current) => ({ ...current, purpose: next as Purpose }))} />
      )}
      {purpose.kind === 'folder' && (
        <>
          <Field label="Folder" value={value('path')} onChange={(next) => set('path', next)}
            helperText="The folder as Faxbot sees it inside its container, such as /scans. Mount your scanner share there." />
          <Field label="Wait for a file to stop changing (seconds)" value={String(value('settle_seconds') || 10)}
            onChange={number('settle_seconds')} />
          {purpose.direction === 'send' && (
            <Alert severity="info" sx={{ mt: 1 }}>
              Put each PDF next to a file with the same name ending in .json that holds the fax number, such as
              {' {"to": "+13035550100"}'}. Faxbot sends each file to each number once.
            </Alert>
          )}
        </>
      )}
      {purpose.direction === 'receive' && (
        <Select label="Mailbox" value={value('mailbox_id')} onChange={(next) => set('mailbox_id', next || null)}
          helperText="Where documents go unless a sidecar file next to them gives a fax number."
          options={[{ id: '', label: 'None: every document needs a sidecar file' },
            ...(choices?.mailboxes ?? []).map((box) => ({ id: box.id, label: box.number ? `${box.label} (${box.number})` : box.label }))]} />
      )}
      {purpose.kind === 'email' && (
        <>
          <Select label="Mail service" value={value('provider') || 'other'} onChange={chooseService} options={SERVICES} />
          {provider && <Alert severity="info" sx={{ mt: 1 }}>{provider.guidance}</Alert>}
          <Field label="Mailbox address" value={value('address')} onChange={(next) => set('address', next)}
            helperText={purpose.direction === 'send' ? 'People email faxes to this address, such as fax@example.com.' : 'Such as scans@example.com.'} />
          <Select label="How Faxbot signs in" value={signIn} onChange={(next) => set('sign_in', next)} options={SIGN_INS} />
          {signIn === 'microsoft_app' && (
            <>
              <Field label="Directory (tenant) ID" value={value('tenant_id')} onChange={(next) => set('tenant_id', next)} />
              <Field label="Application (client) ID" value={value('client_id')} onChange={(next) => set('client_id', next)} />
              <SecretInput fullWidth margin="normal" label="Client secret" value={draft.secret.client_secret ?? ''}
                onChange={(next) => setSecret('client_secret', next)} helperText={keep} />
            </>
          )}
          {signIn === 'google_service_account' && (
            <TextField fullWidth multiline minRows={3} margin="normal" label="Service account key"
              value={draft.secret.service_account ?? ''} onChange={(event) => setSecret('service_account', event.target.value)}
              helperText={keep ?? 'Paste the whole key file. Faxbot keeps it sealed and never shows it again.'} />
          )}
          {signIn === 'password' && (
            <>
              <Field label="Sign-in name" value={value('username')} onChange={(next) => set('username', next)}
                helperText="Leave empty to use the mailbox address." />
              <SecretInput fullWidth margin="normal" label="Password" value={draft.secret.password ?? ''}
                onChange={(next) => setSecret('password', next)} helperText={keep} />
            </>
          )}
          {signIn === 'oauth_refresh' && (
            <>
              <Field label="Token address" value={value('token_url')} onChange={(next) => set('token_url', next)} />
              <Field label="Application (client) ID" value={value('client_id')} onChange={(next) => set('client_id', next)} />
              <SecretInput fullWidth margin="normal" label="Client secret" value={draft.secret.client_secret ?? ''}
                onChange={(next) => setSecret('client_secret', next)} helperText={keep} />
              <SecretInput fullWidth margin="normal" label="Refresh token" value={draft.secret.refresh_token ?? ''}
                onChange={(next) => setSecret('refresh_token', next)} helperText={keep} />
            </>
          )}
          {value('provider') === 'other' && (
            <Box display="grid" gridTemplateColumns={{ xs: '1fr', sm: '2fr 1fr' }} columnGap={2}>
              <Field label="Incoming mail server" value={value('imap_host')} onChange={(next) => set('imap_host', next)} />
              <Field label="Port" value={String(value('imap_port') || 993)} onChange={number('imap_port')} />
            </Box>
          )}
          <Box display="grid" gridTemplateColumns={{ xs: '1fr', sm: '1fr 1fr' }} columnGap={2}>
            <Field label="Folder Faxbot checks" value={value('folder') || 'INBOX'} onChange={(next) => set('folder', next)} />
            <Field label="Folder for handled messages" value={value('processed_folder') || 'Faxbot processed'}
              onChange={(next) => set('processed_folder', next)} />
          </Box>
          {purpose.direction === 'send' && (
            <>
              {value('provider') === 'other' && (
                <>
                  <Box display="grid" gridTemplateColumns={{ xs: '1fr', sm: '2fr 1fr 1fr' }} columnGap={2}>
                    <Field label="Outgoing mail server" value={value('smtp_host')} onChange={(next) => set('smtp_host', next)}
                      helperText="Faxbot answers senders from the mailbox address through this server." />
                    <Field label="Port" value={String(value('smtp_port') || 587)} onChange={number('smtp_port')} />
                    <Select label="Security" value={value('smtp_security') || 'starttls'} onChange={(next) => set('smtp_security', next)}
                      options={[{ id: 'starttls', label: 'STARTTLS' }, { id: 'tls', label: 'TLS' }]} />
                  </Box>
                  <Field label="Mail server that checks senders" value={value('checked_by')} onChange={(next) => set('checked_by', next)}
                    helperText="The name your mail server writes when it checks DKIM and SPF, such as mx.example.com." />
                </>
              )}
              <Typography variant="subtitle2" sx={{ mt: 2 }}>People who may send faxes by email</Typography>
              <Typography variant="body2" color="text.secondary">
                Faxbot sends only from these addresses, only when your mail server confirmed the sender with DKIM or SPF,
                and only while the person may send faxes. The number goes in the subject, such as +13035550100.
              </Typography>
              {draft.senders.map((row, index) => (
                <Box key={index} display="grid" gridTemplateColumns={{ xs: '1fr', sm: '1fr 1fr auto' }} columnGap={2} alignItems="center">
                  <Field label="Email address" value={row.address} onChange={(next) => setDraft((current) => ({
                    ...current, senders: current.senders.map((item, at) => (at === index ? { ...item, address: next } : item)) }))} />
                  <Select label="Faxbot person" value={row.principal_id} onChange={(next) => setDraft((current) => ({
                    ...current, senders: current.senders.map((item, at) => (at === index ? { ...item, principal_id: next } : item)) }))}
                    options={[{ id: '', label: 'Choose a person' }, ...(choices?.people ?? []).map((person) => ({ id: person.id, label: person.name }))]} />
                  <IconButton aria-label={`Remove ${row.address || 'this sender'}`} onClick={() => setDraft((current) => ({
                    ...current, senders: current.senders.filter((_, at) => at !== index) }))}><DeleteIcon /></IconButton>
                </Box>
              ))}
              <Button size="small" onClick={() => setDraft((current) => ({ ...current, senders: [...current.senders, { address: '', principal_id: '' }] }))}>
                Add a person
              </Button>
            </>
          )}
        </>
      )}
      {purpose.direction === 'send' && !existing && (
        <Typography variant="body2" color="text.secondary" sx={{ mt: 2 }}>
          Faxbot gives this connector its own key that may only send faxes. It appears under Access, Keys; pausing the
          connector revokes it.
        </Typography>
      )}
    </FormDialog>
  );
}

function ItemsTable({ items }: { items: ConnectorItem[] }) {
  if (items.length === 0) return <Typography variant="body2" color="text.secondary">Nothing has come through yet.</Typography>;
  return (
    <Box sx={{ overflowX: 'auto' }}>
      <Table size="small">
        <TableHead>
          <TableRow>
            <TableCell>Arrived</TableCell><TableCell>What</TableCell><TableCell>From</TableCell>
            <TableCell>Fax number</TableCell><TableCell>What happened</TableCell><TableCell>Reply</TableCell>
          </TableRow>
        </TableHead>
        <TableBody>
          {items.map((item) => (
            <TableRow key={item.id}>
              <TableCell>{formatServerTime(item.received_at ?? item.created_at)}</TableCell>
              <TableCell>{item.what ?? '-'}</TableCell>
              <TableCell>{item.sender_name ?? item.sender ?? '-'}</TableCell>
              <TableCell>{item.to_number ?? '-'}</TableCell>
              <TableCell>{item.status}</TableCell>
              <TableCell>{item.reply_detail ?? item.reply ?? '-'}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </Box>
  );
}

function countsLine(connector: Connector): string {
  const { items, duplicates, refused } = connector.counts;
  const handled = connector.direction === 'send' ? 'handled' : 'brought in';
  return `${items} ${handled} · ${duplicates} seen again and never handled twice · ${refused} refused`;
}

export default function Connectors({ client, canWrite }: { client: AdminAPIClient; canWrite: boolean }) {
  const [connectors, setConnectors] = useState<Connector[] | null>(null);
  const [choices, setChoices] = useState<ConnectorChoices | null>(null);
  const [items, setItems] = useState<Record<string, ConnectorItem[]>>({});
  const [hidden, setHidden] = useState(false);
  const [editing, setEditing] = useState<Connector | 'new' | null>(null);
  const [removing, setRemoving] = useState<Connector | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setConnectors((await client.listConnectors()).connectors);
    } catch (failure) {
      // Someone who may not read settings sees no page; any other failure is said.
      if (isForbidden(failure)) setHidden(true);
      else setError(failure);
    }
  }, [client]);

  useEffect(() => { void load(); }, [load]);

  const open = async (target: Connector | 'new') => {
    setError(null);
    try {
      setChoices(await client.connectorChoices());
    } catch (failure) {
      setError(failure);
      return;
    }
    setEditing(target);
  };

  const run = async (operation: () => Promise<string | null>) => {
    setBusy(true);
    setError(null);
    try {
      const message = await operation();
      if (message) setNotice(message);
      await load();
      return true;
    } catch (failure) {
      setError(failure);
      return false;
    } finally {
      setBusy(false);
    }
  };

  const save = async (draft: Draft) => {
    const purpose = PURPOSES.find((item) => item.id === draft.purpose) ?? PURPOSES[0];
    const settings = Object.fromEntries(Object.entries(draft.settings).filter(([, value]) => value !== null && value !== ''));
    const secret = Object.fromEntries(Object.entries(draft.secret).filter(([, value]) => value));
    const senders = draft.senders.filter((row) => row.address.trim() && row.principal_id);
    const target = editing;
    const ok = await run(async () => {
      if (target && target !== 'new') {
        await client.updateConnector(target.id, { version: target.version, name: draft.name, settings,
          ...(Object.keys(secret).length ? { secret } : {}),
          ...(target.direction === 'send' && target.kind === 'email' ? { senders } : {}) });
      } else {
        await client.createConnector({ name: draft.name, kind: purpose.kind, direction: purpose.direction, settings,
          ...(purpose.kind === 'email' ? { secret } : {}), ...(purpose.direction === 'send' ? { senders } : {}) });
      }
      return 'Connector saved. Use Test to check it.';
    });
    if (ok) setEditing(null);
  };

  const test = (connector: Connector) => void run(async () => {
    const result = await client.testConnector(connector.id);
    if (!result.ok) throw Object.assign(new Error(result.detail), { name: 'ConnectorTestFailed' });
    return result.detail;
  });

  const pause = (connector: Connector) => void run(async () => {
    if (connector.paused) {
      await client.resumeConnector(connector.id);
      return `${connector.name} is checked again.`;
    }
    await client.pauseConnector(connector.id);
    return `${connector.name} is paused.`;
  });

  const showItems = async (connector: Connector) => {
    if (items[connector.id]) {
      setItems(({ [connector.id]: _closed, ...rest }) => rest);
      return;
    }
    try {
      const found = await client.listConnectorItems({ connector: connector.id, limit: 50 });
      setItems((current) => ({ ...current, [connector.id]: found.items }));
    } catch (failure) {
      setError(failure);
    }
  };

  const remove = async () => {
    if (!removing) return;
    if (await run(async () => { await client.removeConnector(removing.id); return `${removing.name} removed.`; })) setRemoving(null);
  };

  const sorted = useMemo(() => connectors ?? [], [connectors]);
  if (hidden) return null;

  return (
    <Box>
      <Typography variant="h4" component="h1" sx={{ mb: 1 }}>Email and folders</Typography>
      <Typography color="text.secondary" sx={{ mb: 2 }}>
        Mailboxes and folders that bring documents into Faxbot or send faxes. Faxbot files or faxes each document once;
        a copy it sees again is counted, never handled twice.
      </Typography>
      <Notice message={notice} onClose={() => setNotice(null)} />
      {!editing && !removing && <DeliveryError error={error} onClose={() => setError(null)} />}
      {canWrite && (
        <Box mb={2}>
          <Button variant="outlined" onClick={() => void open('new')} sx={{ borderRadius: 2 }}>Add a connector</Button>
        </Box>
      )}
      {connectors === null ? null : sorted.length === 0 ? (
        <EmptyState icon={<AllInboxIcon />} title="No connectors yet"
          text="Add a mailbox or a scanner folder to bring documents in, or let people fax by email." />
      ) : (
        <Stack spacing={2}>
          {sorted.map((connector) => (
            <Card key={connector.id} variant="outlined" sx={{ borderRadius: 2 }}>
              <CardContent>
                <Box display="flex" justifyContent="space-between" flexWrap="wrap" gap={1}>
                  <Box>
                    <Typography variant="subtitle1">
                      {connector.name}{' '}
                      {connector.paused && <Chip size="small" label="Paused" />}
                    </Typography>
                    <Typography variant="body2" color="text.secondary">{connector.what}.</Typography>
                    <Typography variant="body2" color={connector.ok === false ? 'error' : 'text.secondary'}>
                      {connector.status}
                      {connector.last_checked_at ? ` Last checked ${formatServerTime(connector.last_checked_at)}.` : ''}
                    </Typography>
                    <Typography variant="body2" color="text.secondary">{countsLine(connector)}</Typography>
                    {connector.mailbox && (
                      <Typography variant="body2" color="text.secondary">Documents go to {connector.mailbox.label}.</Typography>
                    )}
                    {connector.senders && connector.senders.length > 0 && (
                      <Typography variant="body2" color="text.secondary">
                        May send: {connector.senders.map((sender) => `${sender.name ?? 'someone removed'} (${sender.address})`).join(', ')}
                      </Typography>
                    )}
                  </Box>
                  <Box>
                    <Button size="small" onClick={() => void showItems(connector)}>
                      {items[connector.id] ? 'Hide recent' : 'Recent'}
                    </Button>
                    {canWrite && <Button size="small" onClick={() => test(connector)} disabled={busy}>Test</Button>}
                    {canWrite && <Button size="small" onClick={() => pause(connector)} disabled={busy}>{connector.paused ? 'Resume' : 'Pause'}</Button>}
                    {canWrite && <Button size="small" onClick={() => void open(connector)}>Change</Button>}
                    {canWrite && <Button size="small" color="error" onClick={() => { setError(null); setRemoving(connector); }}>Remove</Button>}
                  </Box>
                </Box>
                {items[connector.id] && <Box mt={2}><ItemsTable items={items[connector.id]} /></Box>}
              </CardContent>
            </Card>
          ))}
        </Stack>
      )}
      {editing && <ConnectorDialog connector={editing} choices={choices} busy={busy} error={error}
        onSave={(draft) => void save(draft)} onClose={() => setEditing(null)} />}
      <ConfirmDialog open={removing !== null} title={`Remove ${removing?.name ?? 'this connector'}?`} danger busy={busy} error={error}
        text="Faxbot stops checking it and revokes its sending key. What it brought in or sent stays listed."
        confirmLabel="Remove" onConfirm={() => void remove()} onCancel={() => setRemoving(null)} />
    </Box>
  );
}

// One line on a sent fax's details: who asked for it, when it came in by email or from a folder.
export function FaxRequestedByItem({ client, jobId }: { client: AdminAPIClient; jobId: string }) {
  const [sentence, setSentence] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    client.faxRequester(jobId).then((found) => { if (live) setSentence(found.sentence); })
      .catch(() => { if (live) setSentence(null); });
    return () => { live = false; };
  }, [client, jobId]);
  if (!sentence) return null;
  return (
    <Box data-testid="job-requested-by" sx={{ px: 2, py: 1 }}>
      <Typography variant="body2" color="text.secondary">Asked for by</Typography>
      <Typography variant="body2">{sentence}</Typography>
    </Box>
  );
}
