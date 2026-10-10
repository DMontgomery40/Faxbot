// Administration → System health → Receiving readiness: whether each of your numbers can receive faxes now, judged from
// receiving evidence only, and which receiver owns each number (receive_readiness.py).
import { useCallback, useEffect, useState } from 'react';
import { Alert, Box, Button, Card, CardContent, MenuItem, Stack, TextField, Typography } from '@mui/material';
import {
  CheckCircle as CheckCircleIcon, Error as ErrorIcon, MoveToInbox as InboxIcon,
  RemoveCircleOutline as OffIcon, Warning as WarningIcon,
} from '@mui/icons-material';
import AdminAPIClient, { AdminAPIError } from '../api/client';
import { siteChecksApi, type Readiness, type ReceivingNumber, type ReceivingStatus } from '../api/siteChecks';

const WORDS: Record<ReceivingStatus, string> = {
  receiving: 'Receiving', attention: 'Needs attention', not_receiving: 'Not receiving', off: 'Not in use',
};
const PLAIN = /^[A-Z][^<>{}]{3,300}[.!?]$/;
const ELSEWHERE = 'elsewhere';

function Icon({ status }: { status: ReceivingStatus }) {
  if (status === 'receiving') return <CheckCircleIcon color="success" titleAccess={WORDS[status]} />;
  if (status === 'attention') return <WarningIcon color="warning" titleAccess={WORDS[status]} />;
  if (status === 'not_receiving') return <ErrorIcon color="error" titleAccess={WORDS[status]} />;
  return <OffIcon color="disabled" titleAccess={WORDS[status]} />;
}

function refusal(error: unknown, fallback: string): string {
  if (error instanceof AdminAPIError && error.status === 403) return 'Your role cannot change this. Sign in with a role that can change settings.';
  if (error instanceof AdminAPIError && error.detail && PLAIN.test(error.detail.trim())) return error.detail.trim();
  return fallback;
}

function OwnerForm({ item, onSave }: { item: ReceivingNumber; onSave: (owner: string, label: string, move: boolean) => void }) {
  const [owner, setOwner] = useState(item.owner ?? '');
  const [label, setLabel] = useState(item.owner === ELSEWHERE ? item.owner_label ?? '' : '');
  const moving = Boolean(item.owner) && owner !== item.owner;
  return (
    <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} sx={{ mt: 1 }}>
      <TextField select size="small" sx={{ minWidth: 200 }} label="Receives its faxes" value={owner}
        onChange={(event) => setOwner(event.target.value)}>
        <MenuItem value="">No one named</MenuItem>
        {item.endpoints.map((endpoint) => <MenuItem key={endpoint.key} value={endpoint.key}>{endpoint.label}</MenuItem>)}
        <MenuItem value={ELSEWHERE}>Somewhere outside Faxbot</MenuItem>
      </TextField>
      {owner === ELSEWHERE && (
        <TextField size="small" label="Which receiver" placeholder="the fax machine at reception" value={label}
          onChange={(event) => setLabel(event.target.value)} />
      )}
      <Button variant="outlined" disabled={owner === (item.owner ?? '') || (owner === ELSEWHERE && !label.trim())}
        onClick={() => onSave(owner, label, moving)}>
        {moving ? 'Move the number here' : 'Save'}
      </Button>
    </Stack>
  );
}

export default function ReceivingReadiness({ client }: { client: AdminAPIClient }) {
  const api = siteChecksApi(client);
  const [data, setData] = useState<Readiness | null>(null);
  const [failed, setFailed] = useState(false);
  const [checking, setChecking] = useState(false);
  const [notice, setNotice] = useState<{ severity: 'success' | 'error'; text: string } | null>(null);

  const load = useCallback(async () => {
    setChecking(true);
    try {
      setData(await siteChecksApi(client).readiness());
      setFailed(false);
    } catch {
      setFailed(true);
    } finally {
      setChecking(false);
    }
  }, [client]);

  useEffect(() => { void load(); }, [load]);

  const save = async (number: string, owner: string, label: string, move: boolean) => {
    setNotice(null);
    try {
      const result = await api.owner({ number, owner: owner || null, label: owner === ELSEWHERE ? label.trim() : null, move });
      setNotice({ severity: 'success', text: result.sentence });
      await load();
    } catch (error) {
      setNotice({ severity: 'error', text: refusal(error, 'The receiver could not be saved. Try again in a moment.') });
    }
  };

  return (
    <Card sx={{ mb: 3, borderRadius: 2 }} data-testid="receiving-readiness">
      <CardContent>
        <Box display="flex" alignItems="center" justifyContent="space-between" gap={1} sx={{ mb: 1 }}>
          <Box display="flex" alignItems="center" gap={1}>
            <InboxIcon color="action" />
            <Typography variant="h6" component="h2">Receiving readiness</Typography>
          </Box>
          <Button size="small" onClick={() => void load()} disabled={checking}>{checking ? 'Checking…' : 'Check again'}</Button>
        </Box>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
          Whether faxes sent to each of your numbers can arrive now. A working internet connection or sending
          route does not count here: only receiving evidence does.
        </Typography>
        {failed && <Alert severity="warning">Faxbot could not check receiving. Check again in a moment.</Alert>}
        {notice && <Alert severity={notice.severity} sx={{ mb: 1.5 }} onClose={() => setNotice(null)}>{notice.text}</Alert>}
        {data && data.numbers.length === 0 && (
          <Typography variant="body2">
            {data.receiving_on ? 'Faxbot receives on no number yet. Add your numbers to your accounts in Delivery setup, Providers & accounts.'
              : 'Receiving faxes is off on this installation.'}
          </Typography>
        )}
        {data && data.numbers.map((item) => (
          <Box key={item.number} sx={{ py: 1.25, borderTop: '1px solid', borderColor: 'divider', '&:first-of-type': { borderTop: 'none' } }}>
            <Box display="flex" gap={1.5} alignItems="flex-start">
              <Box sx={{ pt: 0.25 }}><Icon status={item.status} /></Box>
              <Box sx={{ flex: 1, minWidth: 0 }}>
                <Typography variant="subtitle2" fontWeight={600}>{item.number}: {WORDS[item.status]}</Typography>
                <Typography variant="body2" color="text.secondary">{item.sentence}</Typography>
                {item.checks.map((check) => (
                  <Typography key={check.title + check.sentence} variant="body2" color="text.secondary" sx={{ pl: 1 }}>
                    {check.title}: {check.sentence}
                  </Typography>
                ))}
                <OwnerForm item={item} onSave={(owner, label, move) => void save(item.number, owner, label, move)} />
              </Box>
            </Box>
          </Box>
        ))}
      </CardContent>
    </Card>
  );
}
