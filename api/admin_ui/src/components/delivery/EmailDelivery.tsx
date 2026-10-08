// Email delivery for received faxes: the connectors that email each document,
// with the original PDF attached. Shown in Settings next to Intake defaults.
import { useCallback, useEffect, useState } from 'react';
import { Box, Button, Card, CardContent, FormControlLabel, Stack, Switch, TextField, Typography } from '@mui/material';
import MailIcon from '@mui/icons-material/Mail';
import AdminAPIClient, { isForbidden } from '../../api/client';
import type { EmailConnector, EmailConnectorInput } from '../../api/deliveryTypes';
import SecretInput from '../common/SecretInput';
import { ResponsiveFormSection } from '../common/ResponsiveFormFields';
import { ConfirmDialog, EmptyState, Field, FormDialog } from '../access/AccessViews';
import { DeliveryError, Notice } from './shared';

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

// The list of email deliveries. Hidden only for accounts that may not read them (403).
export default function EmailDelivery({ client, canWrite }: { client: AdminAPIClient; canWrite: boolean }) {
  const [connectors, setConnectors] = useState<EmailConnector[] | null>(null);
  const [hidden, setHidden] = useState(false);
  const [editing, setEditing] = useState<EmailConnector | 'new' | null>(null);
  const [removing, setRemoving] = useState<EmailConnector | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setConnectors((await client.listEmailConnectors()).connectors);
    } catch (failure) {
      // Someone who may not read email deliveries doesn't see this section; any other failure is said.
      if (isForbidden(failure)) setHidden(true);
      else setError(failure);
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

  const remove = async () => {
    if (!removing) return;
    if (await run(async () => { await client.deleteEmailConnector(removing.id); return null; })) setRemoving(null);
  };

  if (hidden) return null;

  return (
    <ResponsiveFormSection title="Email delivery" subtitle="Faxbot emails each received document, with the original PDF attached."
      icon={<MailIcon />}>
      <Box>
        <Notice message={notice} onClose={() => setNotice(null)} />
        {!editing && !removing && <DeliveryError error={error} onClose={() => setError(null)} />}
        {canWrite && (
          <Box mb={2}>
            <Button variant="outlined" onClick={() => { setError(null); setEditing('new'); }} sx={{ borderRadius: 2 }}>Add email delivery</Button>
          </Box>
        )}
        {connectors === null ? null : connectors.length === 0 ? (
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
                        {connector.enabled ? 'Delivering new faxes.' : 'Paused.'}{connector.managed ? ' Set in Email delivery for the whole installation, above.' : ''}
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
      {editing && <ConnectorDialog connector={editing} busy={busy} error={error} onSave={(input) => void save(input)} onClose={() => setEditing(null)} />}
      <ConfirmDialog open={removing !== null} title={`Remove ${removing?.name ?? 'this email delivery'}?`} danger busy={busy} error={error}
        text="New faxes for these numbers stay in your received faxes until another email delivery is set up."
        confirmLabel="Remove" onConfirm={() => void remove()} onCancel={() => setRemoving(null)} />
    </ResponsiveFormSection>
  );
}
