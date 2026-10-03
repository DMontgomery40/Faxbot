// Intake: every received fax and direct delivery, and the email connectors
// that deliver them. Table on desktop, cards on mobile.
import { useCallback, useEffect, useState } from 'react';
import {
  Box, Button, Card, CardContent, Chip, FormControlLabel, Paper, Stack, Switch, Table, TableBody, TableCell,
  TableContainer, TableHead, TableRow, TextField, Typography,
} from '@mui/material';
import InboxIcon from '@mui/icons-material/MoveToInbox';
import MailIcon from '@mui/icons-material/Mail';
import AdminAPIClient from '../api/client';
import type { EmailConnector, EmailConnectorInput, IntakeCounts, IntakeItem } from '../api/deliveryTypes';
import { formatServerTime } from '../api/time';
import SecretInput from './common/SecretInput';
import {
  ConfirmDialog, EmptyState, Field, FormDialog, LoadStateView, ScreenHeader, StatusChip, loadFailure, useSmallScreens,
  type LoadState,
} from './access/AccessViews';
import { DeliveryError, Notice } from './delivery/shared';

const STATE_LABEL: Record<IntakeItem['state'], string> = {
  received: 'Waiting', sending: 'Sending', delivered: 'Delivered', failed: 'Not delivered',
};
const STATE_TONE: Record<IntakeItem['state'], 'default' | 'info' | 'success' | 'error'> = {
  received: 'default', sending: 'info', delivered: 'success', failed: 'error',
};

function itemTitle(item: IntakeItem): string {
  const kind = item.source === 'direct' ? 'Direct delivery' : 'Fax';
  const pages = item.pages ? `, ${item.pages} ${item.pages === 1 ? 'page' : 'pages'}` : '';
  return `${kind} from ${item.from_number || 'an unknown number'}${pages}`;
}

const BLANK: EmailConnectorInput = {
  name: '', enabled: true, match_number: null, host: '', port: 587, security: 'starttls', username: '', password: '',
  from_address: '', recipients: [], subject_template: 'Fax from {from_number}',
};

function ConnectorDialog({ connector, busy, error, onSave, onClose }: {
  connector: EmailConnector | 'new' | null;
  busy: boolean;
  error: unknown;
  onSave: (input: EmailConnectorInput) => void;
  onClose: () => void;
}) {
  const existing = connector !== 'new' && connector !== null ? connector : null;
  const [draft, setDraft] = useState<EmailConnectorInput>(existing ? {
    ...existing, password: null, version: existing.version,
  } : BLANK);
  const [recipients, setRecipients] = useState((existing?.recipients ?? []).join(', '));
  const set = <K extends keyof EmailConnectorInput>(key: K, value: EmailConnectorInput[K]) =>
    setDraft((current) => ({ ...current, [key]: value }));
  const submit = () => onSave({ ...draft, recipients: recipients.split(/[,;\s]+/).filter(Boolean) });
  return (
    <FormDialog open={connector !== null} title={existing ? 'Edit email delivery' : 'Add email delivery'} submitLabel="Save"
      busy={busy} error={null} canSubmit={Boolean(draft.name.trim() && draft.host.trim() && draft.from_address.trim() && recipients.trim())}
      onSubmit={submit} onClose={onClose}>
      <DeliveryError error={error} />
      <Field label="Name" value={draft.name} onChange={(value) => set('name', value)} helperText="For example, Front desk." />
      <Field label="Recipients" value={recipients} onChange={setRecipients} helperText="Email addresses, separated by commas." />
      <Field label="Fax number" value={draft.match_number ?? ''} onChange={(value) => set('match_number', value || null)}
        helperText="Leave empty to deliver faxes to every number." />
      <Box display="grid" gridTemplateColumns={{ xs: '1fr', sm: '2fr 1fr 1fr' }} columnGap={2}>
        <Field label="Email server" value={draft.host} onChange={(value) => set('host', value)} />
        <Field label="Port" value={String(draft.port)} onChange={(value) => set('port', Number(value) || 0)} />
        <TextField select fullWidth margin="normal" label="Security" value={draft.security}
          onChange={(e) => set('security', e.target.value as EmailConnectorInput['security'])} SelectProps={{ native: true }} InputLabelProps={{ shrink: true }}>
          <option value="starttls">STARTTLS</option>
          <option value="tls">TLS</option>
          <option value="none">None</option>
        </TextField>
      </Box>
      <Field label="User name" value={draft.username} onChange={(value) => set('username', value)} />
      <SecretInput fullWidth margin="normal" label="Password" value={draft.password ?? ''} onChange={(value) => set('password', value)}
        helperText={existing?.has_password ? 'Leave empty to keep the saved password.' : undefined} />
      <Field label="Sent from" value={draft.from_address} onChange={(value) => set('from_address', value)} helperText="For example, fax@example.org." />
      <Field label="Subject" value={draft.subject_template} onChange={(value) => set('subject_template', value)}
        helperText="Can include {from_number}, {to_number}, {pages} and {received_at}." />
      <FormControlLabel control={<Switch checked={draft.enabled} onChange={(e) => set('enabled', e.target.checked)} />} label="Deliver new faxes" />
    </FormDialog>
  );
}

export default function Intake({ client, canWrite }: { client: AdminAPIClient; canWrite: boolean }) {
  const { isMobile } = useSmallScreens();
  const [state, setState] = useState<LoadState>('loading');
  const [items, setItems] = useState<IntakeItem[]>([]);
  const [counts, setCounts] = useState<IntakeCounts>({ received: 0, sending: 0, delivered: 0, failed: 0 });
  const [connectors, setConnectors] = useState<EmailConnector[]>([]);
  const [editing, setEditing] = useState<EmailConnector | 'new' | null>(null);
  const [removing, setRemoving] = useState<EmailConnector | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    setState((current) => (current === 'ready' ? current : 'loading'));
    try {
      const [queue, configured] = await Promise.all([client.listIntakeItems(), client.listEmailConnectors()]);
      setItems(queue.items);
      setCounts(queue.counts);
      setConnectors(configured.connectors);
      setState('ready');
    } catch (failure) {
      setState(loadFailure(failure));
    }
  }, [client]);

  useEffect(() => { void load(); }, [load]);

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

  const save = async (input: EmailConnectorInput) => {
    const target = editing;
    const ok = await run(async () => {
      if (target && target !== 'new') await client.updateEmailConnector(target.id, input);
      else await client.createEmailConnector(input);
      return 'Email delivery saved.';
    });
    if (ok) setEditing(null);
  };

  const test = (connector: EmailConnector) => void run(async () => {
    const result = await client.testEmailConnector(connector.id);
    if (!result.ok) throw Object.assign(new Error(result.detail), { name: 'ConnectorTestFailed' });
    return result.detail;
  });

  const retry = (item: IntakeItem) => void run(async () => {
    await client.retryIntakeItem(item.id);
    return 'Faxbot will deliver it shortly.';
  });

  const remove = async () => {
    if (!removing) return;
    if (await run(async () => { await client.deleteEmailConnector(removing.id); return null; })) setRemoving(null);
  };

  const itemAction = (item: IntakeItem) => canWrite && item.needs_action && (
    <Button size="small" onClick={() => retry(item)} disabled={busy} aria-label={`Send ${itemTitle(item)} now`}>Send now</Button>
  );

  return (
    <Box>
      <ScreenHeader title="Intake" onRefresh={() => void load()} busy={state === 'loading'}
        subtitle="Every received fax and direct delivery, and where Faxbot delivers it." />
      <Notice message={notice} onClose={() => setNotice(null)} />
      {!editing && !removing && <DeliveryError error={error} onClose={() => setError(null)} />}
      {state !== 'ready' ? <LoadStateView state={state} onRetry={() => void load()} /> : (
        <>
          <Box display="flex" gap={1} flexWrap="wrap" mb={2}>
            <Chip label={`${counts.received + counts.sending} waiting`} />
            <Chip color="success" label={`${counts.delivered} delivered`} />
            <Chip color={counts.failed ? 'error' : 'default'} label={`${counts.failed} not delivered`} />
          </Box>
          {items.length === 0 ? (
            <EmptyState icon={<InboxIcon />} title="Nothing received yet" text="Received faxes and direct deliveries appear here." />
          ) : isMobile ? (
            <Stack spacing={2}>
              {items.map((item) => (
                <Card key={item.id} variant="outlined" sx={{ borderRadius: 2 }}>
                  <CardContent>
                    <Typography variant="subtitle1">{itemTitle(item)}</Typography>
                    <Typography variant="body2" color="text.secondary">To {item.to_number || 'an unknown number'} · {formatServerTime(item.received_at)}</Typography>
                    <Box my={1}><StatusChip label={STATE_LABEL[item.state]} tone={STATE_TONE[item.state]} /></Box>
                    <Typography variant="body2">{item.status}</Typography>
                    <Box mt={1}>{itemAction(item)}</Box>
                  </CardContent>
                </Card>
              ))}
            </Stack>
          ) : (
            <TableContainer component={Paper} sx={{ borderRadius: 2 }}>
              <Table>
                <TableHead>
                  <TableRow>
                    <TableCell>Received</TableCell>
                    <TableCell>Document</TableCell>
                    <TableCell>To</TableCell>
                    <TableCell>Status</TableCell>
                    <TableCell align="right">Actions</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {items.map((item) => (
                    <TableRow key={item.id} hover>
                      <TableCell>{formatServerTime(item.received_at)}</TableCell>
                      <TableCell>{itemTitle(item)}</TableCell>
                      <TableCell>{item.to_number || '-'}</TableCell>
                      <TableCell>
                        <StatusChip label={STATE_LABEL[item.state]} tone={STATE_TONE[item.state]} />
                        <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>{item.status}</Typography>
                      </TableCell>
                      <TableCell align="right">{itemAction(item)}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableContainer>
          )}

          <Box component="section" mt={4}>
            <Box display="flex" justifyContent="space-between" alignItems="center" mb={1} flexWrap="wrap" gap={1}>
              <Box>
                <Typography variant="h6" component="h2">Email delivery</Typography>
                <Typography variant="body2" color="text.secondary">Faxbot emails each received document, with the original PDF attached.</Typography>
              </Box>
              {canWrite && <Button variant="outlined" onClick={() => { setError(null); setEditing('new'); }} sx={{ borderRadius: 2 }}>Add email delivery</Button>}
            </Box>
            {connectors.length === 0 ? (
              <EmptyState icon={<MailIcon />} title="No email delivery yet" text="Add an email server and the inbox where staff read faxes." />
            ) : (
              <Stack spacing={2}>
                {connectors.map((connector) => (
                  <Card key={connector.id} variant="outlined" sx={{ borderRadius: 2 }}>
                    <CardContent>
                      <Box display="flex" justifyContent="space-between" flexWrap="wrap" gap={1}>
                        <Box>
                          <Typography variant="subtitle1">{connector.name}</Typography>
                          <Typography variant="body2" color="text.secondary">
                            To {connector.recipients.join(', ')} · {connector.match_number ? `faxes to ${connector.match_number}` : 'all fax numbers'}
                          </Typography>
                          <Typography variant="body2" color="text.secondary">
                            {connector.enabled ? 'Delivering new faxes.' : 'Paused.'}{connector.managed ? ' Set in the installation settings.' : ''}
                          </Typography>
                        </Box>
                        <Box>
                          {canWrite && <Button size="small" onClick={() => test(connector)} disabled={busy}>Send test email</Button>}
                          {canWrite && !connector.managed && <Button size="small" onClick={() => { setError(null); setEditing(connector); }}>Edit</Button>}
                          {canWrite && !connector.managed && <Button size="small" color="error" onClick={() => { setError(null); setRemoving(connector); }}>Remove</Button>}
                        </Box>
                      </Box>
                    </CardContent>
                  </Card>
                ))}
              </Stack>
            )}
          </Box>
        </>
      )}
      {editing && <ConnectorDialog connector={editing} busy={busy} error={error} onSave={(input) => void save(input)} onClose={() => setEditing(null)} />}
      <ConfirmDialog open={removing !== null} title={`Remove ${removing?.name ?? 'this email delivery'}?`} danger busy={busy} error={error}
        text="New faxes for these numbers wait in the queue until another email delivery is set up."
        confirmLabel="Remove" onConfirm={() => void remove()} onCancel={() => setRemoving(null)} />
    </Box>
  );
}
