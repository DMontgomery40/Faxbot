// Partners → a partner → Send once: the partner's intake files one copy of your fax for each of its numbers, or
// yours files theirs. Off until both sides sign: the receiving side offers its numbers, the sender accepts, either
// side ends it.
import { useCallback, useEffect, useState } from 'react';
import {
  Box, Button, CircularProgress, Dialog, DialogActions, DialogContent, DialogTitle, Divider, Paper, Stack, TextField,
  Typography,
} from '@mui/material';
import AdminAPIClient from '../../api/client';
import type { DirectPartner, SendOnceAgreement } from '../../api/deliveryTypes';
import { StatusChip } from '../access/AccessViews';
import { DeliveryError, Notice } from './shared';

const STATE: Record<SendOnceAgreement['state'], { label: string; tone: 'success' | 'warning' | 'default' | 'info' }> = {
  offered: { label: 'Offered', tone: 'info' },
  accepting: { label: 'Accepting', tone: 'warning' },
  active: { label: 'Active', tone: 'success' },
  withdrawn: { label: 'Ended', tone: 'default' },
};

export function parseNumbers(text: string): string[] {
  return text.split(/[\s,;]+/).map((part) => part.trim()).filter(Boolean);
}

function Agreement({ item, canWrite, busy, onAccept, onEnd }: {
  item: SendOnceAgreement; canWrite: boolean; busy: boolean; onAccept: () => void; onEnd: () => void;
}) {
  const state = STATE[item.state];
  return (
    <Paper variant="outlined" sx={{ p: 2, borderRadius: 2 }} data-testid="send-once-agreement">
      <Box display="flex" gap={1} alignItems="center" flexWrap="wrap" mb={1}>
        <StatusChip label={state.label} tone={state.tone} />
        <Typography variant="subtitle1">{item.summary}</Typography>
      </Box>
      <Typography variant="body2">{item.status}</Typography>
      <Box mt={1}>
        {(item.placements ?? item.numbers.map((number) => ({ fax_number: number, mailbox: null, held: null }))).map((place) => (
          <Typography key={place.fax_number} variant="body2" color="text.secondary">
            {place.mailbox ? `${place.fax_number}: ${place.mailbox}` : place.held ?? place.fax_number}
          </Typography>
        ))}
      </Box>
      {canWrite && item.state !== 'withdrawn' && (
        <Box display="flex" gap={1} mt={1.5} flexWrap="wrap">
          {item.role === 'sender' && item.state === 'offered' && (
            <Button size="small" variant="contained" onClick={onAccept} disabled={busy}>Accept</Button>
          )}
          <Button size="small" color="error" onClick={onEnd} disabled={busy}>End agreement</Button>
        </Box>
      )}
    </Paper>
  );
}

export default function PartnerSendOnce({ client, partner, canWrite, open, onClose }: {
  client: AdminAPIClient;
  partner: DirectPartner;
  canWrite: boolean;
  open: boolean;
  onClose: () => void;
}) {
  const name = partner.organization;
  const [items, setItems] = useState<SendOnceAgreement[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [offering, setOffering] = useState(false);
  const [numbers, setNumbers] = useState('');
  const [intake, setIntake] = useState('Central intake');

  const load = useCallback(async () => {
    try {
      setItems((await client.listSendOnce()).agreements.filter((item) => item.peer_id === partner.id));
    } catch (failure) {
      setError(failure);
      setItems([]);
    }
  }, [client, partner.id]);

  useEffect(() => { if (open) void load(); }, [open, load]);

  const run = async (operation: () => Promise<string | null | undefined>) => {
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

  const offered = (items ?? []).some((item) => item.role === 'receiver' && item.state !== 'withdrawn');

  const offer = async () => {
    const ok = await run(async () => (await client.offerSendOnce(partner.id, parseNumbers(numbers), intake.trim())).detail);
    if (ok) setOffering(false);
  };

  return (
    <Dialog open={open} onClose={onClose} fullWidth maxWidth="md" aria-labelledby="send-once-title">
      <DialogTitle id="send-once-title">Send once with {name}</DialogTitle>
      <DialogContent>
        <Typography variant="body2" color="text.secondary" mb={2}>
          An organization that files its faxes centrally can name the numbers its intake files for. Faxes to those
          numbers then go directly to the intake, and one document for several of them goes over the internet once.
        </Typography>
        <Notice message={notice} onClose={() => setNotice(null)} />
        <DeliveryError error={error} onClose={() => setError(null)} />
        {items === null ? <CircularProgress size={24} /> : (
          <Stack spacing={2}>
            {items.length === 0 && (
              <Typography variant="body2" data-testid="send-once-none">No send-once agreement with {name} yet.</Typography>
            )}
            {items.map((item) => (
              <Agreement key={item.id} item={item} canWrite={canWrite} busy={busy}
                onAccept={() => void run(async () => (await client.acceptSendOnce(item.id)).detail)}
                onEnd={() => void run(async () => (await client.endSendOnce(item.id)).detail)} />
            ))}
          </Stack>
        )}

        {canWrite && !offered && items !== null && !offering && (
          <Box mt={2}>
            <Button size="small" onClick={() => setOffering(true)} disabled={busy}>Offer your intake to {name}</Button>
          </Box>
        )}
        {offering && (
          <Box mt={3} data-testid="send-once-offer">
            <Divider sx={{ mb: 2 }} />
            <Typography variant="subtitle1" gutterBottom>Your intake files {name}'s faxes for these numbers</Typography>
            <TextField fullWidth size="small" label="Your fax numbers" value={numbers} sx={{ mb: 2 }}
              onChange={(event) => setNumbers(event.target.value)}
              helperText="With the country code, separated by spaces or commas. Your receiving rules file each one." />
            <TextField fullWidth size="small" label="Intake name" value={intake}
              onChange={(event) => setIntake(event.target.value)} helperText={`${name} sees this name.`} />
            <Box display="flex" gap={1} mt={2}>
              <Button variant="contained" onClick={() => void offer()}
                disabled={busy || parseNumbers(numbers).length === 0 || !intake.trim()}>Offer</Button>
              <Button onClick={() => setOffering(false)} disabled={busy}>Cancel</Button>
            </Box>
          </Box>
        )}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Close</Button>
      </DialogActions>
    </Dialog>
  );
}
