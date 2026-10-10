// Recipients → Recipients: registered senders. Some recipients, such as banks, accept a fax as an instruction only
// from the number you registered with them. Faxes to a recipient here go only from its registered trunk, showing
// the registered caller ID and station ID; when that trunk cannot send them, they wait in Sent. Sent details show
// the sender's evidence for such a fax and record a request for the original.
import { useCallback, useEffect, useState } from 'react';
import {
  Alert, Box, Button, Divider, ListItem, ListItemText, MenuItem, Paper, Stack, Table, TableBody, TableCell,
  TableContainer, TableHead, TableRow, TextField, Typography,
} from '@mui/material';
import AdminAPIClient, { AdminAPIError } from '../../api/client';
import { formatLocalDate } from '../../api/time';
import { Field, FormDialog } from '../access/AccessViews';
import LoadFailed, { saysFailure } from '../common/LoadFailed';
import { DeliveryError } from './shared';

export interface SenderPin {
  recipient: string;
  account: string;
  account_label: string;
  caller_id: string;
  station_id: string;
  note: string | null;
  recorded_by: string | null;
  recorded_at: string | null;
  ready: boolean;
  sentence: string;
}

export interface PinTrunk {
  account: string;
  label: string;
  caller_id: string | null;
  station_id: string | null;
}

interface Pins {
  pins: SenderPin[];
  trunks: PinTrunk[];
}

export interface SenderEvidence {
  job_id: string;
  recipient: string;
  pin: { caller_id: string; station_id: string; account: string };
  kept: string[];
  calls: Array<{ caller_id: string | null; answering_station: string | null; pages: number | null;
    fax_status: string | null; disposition: string; matches_pin: boolean }>;
  original: 'requested' | 'sent' | 'cancelled' | null;
}

function AddPin({ client, trunks, onSaved }: { client: AdminAPIClient; trunks: PinTrunk[]; onSaved: (next: Pins) => void }) {
  const [open, setOpen] = useState(false);
  const [recipient, setRecipient] = useState('');
  const [account, setAccount] = useState(trunks[0]?.account ?? '');
  const [caller, setCaller] = useState(trunks[0]?.caller_id ?? '');
  const [station, setStation] = useState('');
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const choose = (key: string) => {
    setAccount(key);
    setCaller(trunks.find((item) => item.account === key)?.caller_id ?? '');
  };
  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      onSaved(await client.call<Pins>({ method: 'PUT', path: `/routing/sender-pins/${encodeURIComponent(recipient.trim())}`,
        body: { account, caller_id: caller.trim(), station_id: station.trim() || null, note: note.trim() } }));
      setOpen(false);
      setRecipient('');
      setNote('');
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <Button size="small" variant="outlined" disabled={!trunks.length} onClick={() => { setError(null); setOpen(true); }}>
        Add a registered sender
      </Button>
      <FormDialog open={open} title="Add a registered sender" submitLabel="Save" busy={busy}
        canSubmit={Boolean(recipient.trim() && account && caller.trim())} onSubmit={() => void save()}
        onClose={() => setOpen(false)}>
        <DeliveryError error={error} />
        <Typography variant="body2" sx={{ mt: 1 }}>
          Faxes to this recipient will go only from this trunk, showing this caller ID and station ID. When it cannot
          send them, they wait in Sent and never go from another number.
        </Typography>
        <Field label="Recipient's fax number" value={recipient} onChange={setRecipient} placeholder="+902122220000" />
        <TextField select fullWidth margin="normal" label="Trunk registered with it" value={account}
          onChange={(event) => choose(event.target.value)}>
          {trunks.map((item) => (
            <MenuItem key={item.account} value={item.account}>
              {item.label}{item.caller_id ? ` (shows ${item.caller_id})` : ''}
            </MenuItem>
          ))}
        </TextField>
        <Field label="Registered caller ID" value={caller} onChange={setCaller} />
        <Field label="Registered station ID (optional)" value={station} onChange={setStation}
          helperText="Only if the recipient registered a station ID other than the caller ID." />
        <Field label="Note" value={note} onChange={setNote} helperText="Where it is registered, such as the bank's form." />
      </FormDialog>
    </>
  );
}

export default function RegisteredSenders({ client, canWrite }: { client: AdminAPIClient; canWrite: boolean }) {
  const [pins, setPins] = useState<Pins | null>(null);
  const [failed, setFailed] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const load = useCallback(async () => {
    try {
      setPins(await client.call<Pins>({ method: 'GET', path: '/routing/sender-pins' }));
      setFailed(false);
    } catch (failure) {
      setFailed(saysFailure(failure));
    }
  }, [client]);
  useEffect(() => { void load(); }, [load]);
  const remove = async (pin: SenderPin) => {
    setError(null);
    try {
      setPins(await client.call<Pins>({ method: 'DELETE', path: `/routing/sender-pins/${encodeURIComponent(pin.recipient)}` }));
    } catch (failure) {
      setError(failure);
    }
  };
  if (failed) return <LoadFailed testId="sender-pins-unread" text="Registered senders could not be loaded. Try again." />;
  if (!pins) return null;
  return (
    <Box component="section" mt={4} data-testid="registered-senders">
      <Typography variant="h6" component="h2">Registered senders</Typography>
      <Typography variant="body2" color="text.secondary" mb={1}>
        Some recipients, such as banks, accept a faxed instruction only from the number you registered with them.
        Faxes to a recipient here go only from its registered trunk; when that trunk cannot send them, they wait in
        Sent.
      </Typography>
      <DeliveryError error={error} />
      {pins.pins.length === 0 ? (
        <Typography variant="body2">No registered senders.</Typography>
      ) : (
        <TableContainer component={Paper} sx={{ borderRadius: 2 }}>
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell>Recipient</TableCell>
                <TableCell>Trunk</TableCell>
                <TableCell>Caller ID</TableCell>
                <TableCell>Station ID</TableCell>
                <TableCell>Now</TableCell>
                <TableCell align="right">Actions</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {pins.pins.map((pin) => (
                <TableRow key={pin.recipient}>
                  <TableCell>{pin.recipient}</TableCell>
                  <TableCell>{pin.account_label}</TableCell>
                  <TableCell>{pin.caller_id}</TableCell>
                  <TableCell>{pin.station_id}</TableCell>
                  <TableCell>
                    {pin.ready ? 'Ready' : <Alert severity="warning" sx={{ py: 0 }}>{pin.sentence}</Alert>}
                    {pin.recorded_at && (
                      <Typography variant="caption" color="text.secondary" display="block">
                        Added on {formatLocalDate(pin.recorded_at)}{pin.note ? `: ${pin.note}` : ''}
                      </Typography>
                    )}
                  </TableCell>
                  <TableCell align="right">
                    {canWrite && <Button size="small" color="error" onClick={() => void remove(pin)}
                      aria-label={`Remove the registered sender for ${pin.recipient}`}>Remove</Button>}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableContainer>
      )}
      {canWrite && <Stack direction="row" mt={1}><AddPin client={client} trunks={pins.trunks} onSaved={setPins} /></Stack>}
    </Box>
  );
}

const ORIGINAL_TEXT = {
  requested: 'The recipient asked for the original.',
  sent: 'The original was sent.',
  cancelled: 'The request for the original was withdrawn.',
};

// Sent details: the sender's evidence for a fax to a registered-sender recipient; nothing for any other fax.
export function SenderEvidenceItem({ client, jobId }: { client: AdminAPIClient; jobId: string }) {
  const [evidence, setEvidence] = useState<SenderEvidence | null>(null);
  const [unread, setUnread] = useState(false);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    let live = true;
    setEvidence(null);
    setUnread(false);
    client.call<SenderEvidence>({ method: 'GET', path: `/routing/faxes/${encodeURIComponent(jobId)}/sender-evidence` })
      .then((value) => { if (live) setEvidence(value); })
      .catch((failure) => {
        if (!live) return;
        const absent = failure instanceof AdminAPIError && failure.status === 404;
        setUnread(!absent && saysFailure(failure));
      });
    return () => { live = false; };
  }, [client, jobId]);
  const record = async (state: 'requested' | 'sent') => {
    setError(null);
    try {
      setEvidence(await client.call<SenderEvidence>({ method: 'POST',
        path: `/routing/faxes/${encodeURIComponent(jobId)}/original`, body: { state, note: '' } }));
    } catch (failure) {
      setError(failure);
    }
  };
  if (!evidence && !unread) return null;
  if (!evidence) {
    return (<><Divider /><ListItem><ListItemText primary="Sender's evidence"
      secondary={<LoadFailed testId="sender-evidence-unread" text="The sender's evidence could not be loaded. Try again." />} />
    </ListItem></>);
  }
  const answered = evidence.calls.map((call) => call.answering_station).filter(Boolean);
  return (
    <>
      <Divider />
      <ListItem data-testid="sender-evidence" alignItems="flex-start">
        <ListItemText primary="Sender's evidence" secondary={(
          <>
            {`Sent from ${evidence.pin.caller_id}, the number registered with ${evidence.recipient}.`}
            {answered.length ? ` Answered by ${answered.join(', ')}.` : ''}
            {` Kept: ${evidence.kept.length ? evidence.kept.join(' and ') : 'no files'}.`}
            {evidence.original ? ` ${ORIGINAL_TEXT[evidence.original]}` : ''}
            <DeliveryError error={error} />
            <Box component="span" display="block" mt={1}>
              {evidence.original !== 'requested' && evidence.original !== 'sent' && (
                <Button size="small" onClick={() => void record('requested')}>The recipient asked for the original</Button>
              )}
              {evidence.original === 'requested' && (
                <Button size="small" onClick={() => void record('sent')}>The original was sent</Button>
              )}
            </Box>
          </>
        )} />
      </ListItem>
    </>
  );
}
