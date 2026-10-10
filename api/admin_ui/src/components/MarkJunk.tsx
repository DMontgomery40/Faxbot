import { useState } from 'react';
import { Button, Dialog, DialogActions, DialogContent, DialogTitle, TextField, Typography } from '@mui/material';
import AdminAPIClient, { AdminAPIError } from '../api/client';

interface MarkJunkProps {
  client: AdminAPIClient;
  inboundId: string;
  // The sender as the list shows it (only its last four digits).
  from: string;
  onDone: (text: string) => void;
}

// "Mark sender as junk" on a received fax: their calls are turned away before answering for 90 days
// (Delivery setup → Blocked senders lists them and unblocks with one click).
function MarkJunk({ client, inboundId, from, onDone }: MarkJunkProps) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const result = await client.blockSender({ inbound_id: inboundId, reason: reason.trim() });
      setOpen(false);
      setReason('');
      onDone(`Calls from ${result.entry.number} are now turned away before answering, for 90 days.`);
    } catch (failure) {
      setError(failure instanceof AdminAPIError && failure.detail ? failure.detail : 'The sender was not blocked. Try again.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <Button size="small" onClick={() => setOpen(true)} aria-label={`Mark the sender ${from} as junk`}>Mark as junk</Button>
      <Dialog open={open} onClose={() => setOpen(false)} fullWidth maxWidth="sm">
        <DialogTitle>Mark sender as junk</DialogTitle>
        <DialogContent>
          <Typography variant="body2" sx={{ mb: 2 }}>
            Faxbot turns away calls from {from} before answering for 90 days, so they cost nothing. You can unblock them
            under Delivery setup, Blocked senders.
          </Typography>
          <TextField autoFocus fullWidth label="Why it is junk" value={reason} inputProps={{ maxLength: 200 }}
            onChange={(event) => setReason(event.target.value)} error={!!error} helperText={error ?? 'For example: unsolicited offers.'} />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setOpen(false)}>Cancel</Button>
          <Button variant="contained" disabled={busy || !reason.trim()} onClick={() => { void submit(); }}>Block sender</Button>
        </DialogActions>
      </Dialog>
    </>
  );
}

export default MarkJunk;
