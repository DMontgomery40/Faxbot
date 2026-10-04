import { useEffect, useRef, useState } from 'react';
import { Alert, Box, Button, CircularProgress, Paper, Stack, TextField, Typography } from '@mui/material';
import AdminAPIClient, { FaxRefusedError } from '../api/client';
import type { FaxJob, NumberFormat } from '../api/types';
import { numberPlaceholder } from './common/numbers';
import { providerLabel } from '../providerLabels';
import { testPagePdf } from './testPage';

// The optional last step of the Setup Wizard: one test fax the person asks
// for (never sent by itself, never sent again), followed live; and a wait for
// a fax sent to this installation's number.

interface WizardTestFaxProps {
  client: AdminAPIClient;
  sending: string;
  receiving: string;
  // The trunk's fax numbers, when the SIP trunk receives.
  numbers: string[];
  numberFormat?: NumberFormat | null;
  // The installation's name for the test page, when one is set.
  installation?: string | null;
  pollMs?: number;
  sendWaitMs?: number;
  receiveWaitMs?: number;
}

type Notice = { severity: 'success' | 'info' | 'warning' | 'error'; text: string };


const IN_PROGRESS = new Set(['queued', 'ready', 'preparing', 'submitting', 'in_progress', 'sending', 'pending']);

function stateOf(job: FaxJob): string {
  return String(job.delivery_state || job.status || '').toLowerCase();
}

// One sentence for where the test fax ended up; null while it is still on its way.
export function testOutcome(job: FaxJob): Notice | null {
  const state = stateOf(job);
  if (IN_PROGRESS.has(state)) return null;
  if (state === 'success') return { severity: 'success', text: 'Sent: the receiving machine confirmed the test page.' };
  if (state === 'held') return { severity: 'warning', text: 'The test fax is held because sending is turned off in Settings; it will not be sent.' };
  if (state === 'cancelled') return { severity: 'info', text: 'The test fax was cancelled.' };
  if (state === 'reconciliation_required') {
    return { severity: 'warning', text: 'Faxbot could not confirm whether the test fax arrived; check Jobs before sending another.' };
  }
  return { severity: 'error', text: job.error?.trim() || 'The test fax was not sent.' };
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

export default function WizardTestFax({ client, sending, receiving, numbers, numberFormat, installation, pollMs = 2000,
  sendWaitMs = 300000, receiveWaitMs = 300000 }: WizardTestFaxProps) {
  const [number, setNumber] = useState('');
  const [sendingNow, setSendingNow] = useState(false);
  const [progress, setProgress] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<Notice | null>(null);
  // The carrier's own cost sentence for the test fax, once it is known.
  const [cost, setCost] = useState<string | null>(null);
  const [audio, setAudio] = useState<'offer' | 'switched' | null>(null);
  const [waiting, setWaiting] = useState(false);
  const [arrival, setArrival] = useState<Notice | null>(null);
  const alive = useRef(true);
  const waitEpoch = useRef(0);
  useEffect(() => () => { alive.current = false; waitEpoch.current += 1; }, []);

  // After a failed test over the trunk: Faxbot may have switched to audio fax already, or offers it.
  const afterFailure = async () => {
    if (sending !== 'sip') return;
    try {
      const status = await client.getSipStatus();
      if (!alive.current) return;
      if (status.t38_off_reason === 'no_data_back') setAudio('switched');
      else if (status.suggest_audio) setAudio('offer');
    } catch {
      // The outcome sentence already says what happened.
    }
  };

  const sendTest = async () => {
    const destination = number.trim();
    if (!destination || sendingNow) return;
    setSendingNow(true);
    setOutcome(null);
    setCost(null);
    setAudio(null);
    setProgress('Sending the test fax…');
    try {
      const page = testPagePdf({
        sentAt: new Date().toLocaleString(undefined, { dateStyle: 'long', timeStyle: 'long' }),
        installation, provider: providerLabel(sending), destination,
      });
      const file = new File([page], 'faxbot-test-page.pdf', { type: 'application/pdf' });
      // One key per request: the server never makes a second fax from it.
      const key = typeof crypto !== 'undefined' && 'randomUUID' in crypto ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
      const accepted = await client.sendFax(destination, file, { idempotencyKey: key });
      const started = Date.now();
      while (alive.current) {
        await sleep(pollMs);
        if (!alive.current) return;
        let job: FaxJob;
        try {
          job = await client.getJob(accepted.id);
        } catch {
          continue;
        }
        const found = testOutcome(job);
        if (found) {
          setOutcome(found);
          client.getFaxCost(accepted.id)
            .then((value) => { if (alive.current && value?.summary) setCost(value.summary); })
            .catch(() => undefined);
          if (found.severity === 'error') await afterFailure();
          return;
        }
        if (Date.now() - started >= sendWaitMs) {
          setOutcome({ severity: 'info', text: 'The test fax is still on its way; follow it in Sent.' });
          return;
        }
        const through = job.backend || sending;
        setProgress(stateOf(job) !== 'in_progress' ? 'Sending the test fax…'
          : through === 'sip' ? 'The call is in progress…' : `${providerLabel(through)} is sending the test page…`);
      }
    } catch (error) {
      if (!alive.current) return;
      setOutcome({ severity: error instanceof FaxRefusedError ? 'error' : 'warning',
        text: error instanceof Error ? error.message : 'The test fax was not accepted.' });
    } finally {
      if (alive.current) {
        setSendingNow(false);
        setProgress(null);
      }
    }
  };

  const useAudioFax = async () => {
    try {
      const current = await client.getSettings();
      await client.updateSettings({ expected_revision_id: current._meta?.desired_revision_id, sip_t38_enabled: false });
      await client.applySipTrunk();
      if (alive.current) setAudio('switched');
    } catch {
      if (alive.current) setOutcome({ severity: 'error', text: 'Audio fax could not be turned on. Try again in the fax line settings.' });
    }
  };

  const waitForFax = async () => {
    const epoch = ++waitEpoch.current;
    setWaiting(true);
    setArrival(null);
    try {
      const known = new Set((await client.listInbound()).map((fax) => fax.id));
      const started = Date.now();
      while (epoch === waitEpoch.current) {
        await sleep(pollMs);
        if (epoch !== waitEpoch.current) return;
        try {
          const fresh = (await client.listInbound()).find((fax) => !known.has(fax.id));
          if (fresh) {
            setArrival({ severity: 'success', text: `A fax${fresh.fr ? ` from ${fresh.fr}` : ''} arrived; it is in your received faxes.` });
            return;
          }
        } catch {
          // Keep waiting; the next check may work.
        }
        if (Date.now() - started >= receiveWaitMs) {
          setArrival({ severity: 'info', text: 'No fax arrived in 5 minutes. Check the fax line, then wait again.' });
          return;
        }
      }
    } catch {
      if (epoch === waitEpoch.current) setArrival({ severity: 'error', text: 'Received faxes could not be read. Try again.' });
    } finally {
      if (epoch === waitEpoch.current) setWaiting(false);
    }
  };

  const stopWaiting = () => {
    waitEpoch.current += 1;
    setWaiting(false);
  };

  const receiveText = receiving === 'sip'
    ? (numbers.length
      ? `Send a fax to ${numbers.join(' or ')} from any fax machine or service; it will show up in your received faxes.`
      : 'Add your fax number to your fax line first, then send a fax to it from any fax service.')
    : `Send a fax to your ${providerLabel(receiving)} fax number from any fax machine or service; it will show up in your received faxes.`;

  return (
    <Stack spacing={2} data-testid="wizard-test-fax">
      {sending && (
        <Paper variant="outlined" sx={{ p: 2 }}>
          <Typography variant="subtitle1" component="h3">Send a test fax (optional)</Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
            Faxbot sends one test page to the number you enter, once; your provider may charge for it.
          </Typography>
          <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1}>
            <TextField size="small" label="Fax number" type="tel" value={number} placeholder={numberPlaceholder(numberFormat ?? null)}
              onChange={(event) => setNumber(event.target.value)} disabled={sendingNow} />
            <Button variant="contained" onClick={() => { void sendTest(); }} disabled={sendingNow || !number.trim()}>
              Send a test fax
            </Button>
          </Stack>
          {progress && (
            <Box role="status" sx={{ display: 'flex', alignItems: 'center', gap: 1, mt: 1 }}>
              <CircularProgress size={16} /><Typography variant="body2">{progress}</Typography>
            </Box>
          )}
          {outcome && <Alert severity={outcome.severity} sx={{ mt: 1 }} data-testid="test-fax-outcome">{outcome.text}</Alert>}
          {cost && <Typography variant="caption" color="text.secondary" display="block" sx={{ mt: 0.5 }}>{cost}</Typography>}
          {audio === 'switched' && (
            <Alert severity="info" sx={{ mt: 1 }}>Faxbot now uses audio fax for new calls; send another test when you are ready.</Alert>
          )}
          {audio === 'offer' && (
            <Box sx={{ mt: 1 }}>
              <Typography variant="body2">Audio fax may still work when T.38 data cannot come back through your network.</Typography>
              <Button size="small" variant="outlined" sx={{ mt: 1 }} onClick={() => { void useAudioFax(); }}>Use audio fax for new calls</Button>
            </Box>
          )}
        </Paper>
      )}
      {receiving && (
        <Paper variant="outlined" sx={{ p: 2 }}>
          <Typography variant="subtitle1" component="h3">Receive a test fax (optional)</Typography>
          <Typography variant="body2" sx={{ mb: 1 }} data-testid="receive-instructions">{receiveText}</Typography>
          <Stack direction="row" spacing={1}>
            <Button variant="outlined" onClick={() => { void waitForFax(); }} disabled={waiting}>Wait for a received fax</Button>
            {waiting && <Button onClick={stopWaiting}>Stop waiting</Button>}
          </Stack>
          {waiting && (
            <Box role="status" sx={{ display: 'flex', alignItems: 'center', gap: 1, mt: 1 }}>
              <CircularProgress size={16} /><Typography variant="body2">Waiting for a fax…</Typography>
            </Box>
          )}
          {arrival && <Alert severity={arrival.severity} sx={{ mt: 1 }} data-testid="test-receive-outcome">{arrival.text}</Alert>}
        </Paper>
      )}
    </Stack>
  );
}
