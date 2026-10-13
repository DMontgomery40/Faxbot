// Recipients → Partners: line windows and preflight with partners that take part (direct/admission.py,
// direct/preflight.py). Your receiving side's settings, and per partner: whether you take part, the document classes
// you refuse from it, the windows issued and received, the preflight answers, and the measured retry picture.
import { useCallback, useEffect, useState } from 'react';
import {
  Alert, Box, Button, Card, CardContent, FormControlLabel, MenuItem, Stack, Switch, TextField, Typography,
} from '@mui/material';
import type AdminAPIClient from '../../api/client';
import { AdminAPIError } from '../../api/client';
import type { AdmissionPartner, PartnerAdmission as View } from '../../api/admissionTypes';
import { formatServerTime } from '../../api/time';

type Client = Pick<AdminAPIClient, 'partnerAdmission' | 'saveAdmissionSettings' | 'saveAdmissionPartner'>;

function problem(error: unknown, fallback: string): string {
  if (error instanceof AdminAPIError && error.status === 403) return 'You do not have permission to do this.';
  if (error instanceof AdminAPIError && error.detail) return error.detail;
  return fallback;
}

const OUTCOME = { granted: 'Window', not_taking_part: 'Not taking part', no_answer: 'No answer' } as const;

function grantLine(item: AdmissionPartner['grants'][number]): string {
  const side = item.direction === 'issued' ? 'You granted' : 'You received';
  if (item.outcome !== 'granted') return `${side}: ${OUTCOME[item.outcome]} (${formatServerTime(item.created_at)})`;
  return `${side} a ${item.duration_class} window from ${formatServerTime(item.earliest)} to ${formatServerTime(item.latest)}`;
}

function preflightLine(item: AdmissionPartner['preflights'][number]): string {
  const said = item.accepts === 1 ? 'accept' : item.accepts === 0 ? 'refuse' : 'did not say whether they take';
  const who = item.direction === 'asked' ? 'They' : 'You';
  const where = item.mailbox ? ` at ${item.mailbox}` : '';
  return `${who} ${said} ${item.document_class} faxes${where}${item.reason && item.accepts === 0 ? `: ${item.reason}` : ''}`
    + ` (${formatServerTime(item.created_at)})`;
}

function PartnerSection({ client, partner, canWrite, onSaved }: {
  client: Client; partner: AdmissionPartner; canWrite: boolean; onSaved: (next: View) => void;
}) {
  const [classes, setClasses] = useState(partner.refused_classes.join(', '));
  const [reason, setReason] = useState(partner.refusal_reason ?? '');
  const [message, setMessage] = useState<string | null>(null);
  const save = async (takePart: boolean) => {
    try {
      onSaved(await client.saveAdmissionPartner(partner.peer_id, {
        take_part: takePart, refused_classes: classes.split(',').map((item) => item.trim()).filter(Boolean),
        refusal_reason: reason.trim() || null }));
      setMessage(null);
    } catch (failure) {
      setMessage(problem(failure, 'This could not be saved. Try again.'));
    }
  };
  return (
    <Box component="section" aria-label={`Line windows with ${partner.organization}`} sx={{ mt: 2 }}>
      <Typography variant="subtitle2">{partner.organization}</Typography>
      <FormControlLabel
        control={<Switch checked={partner.take_part} disabled={!canWrite} onChange={(event) => void save(event.target.checked)} />}
        label="Take part: ask before paying to send by phone, and wait for line windows" />
      {canWrite && (
        <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} sx={{ my: 1 }}>
          <TextField size="small" label="Document classes you refuse from them" value={classes}
            placeholder="billing, marketing" onChange={(event) => setClasses(event.target.value)} />
          <TextField size="small" label="The reason they see" value={reason}
            onChange={(event) => setReason(event.target.value)} />
          <Button size="small" onClick={() => void save(partner.take_part)}>Save</Button>
        </Stack>
      )}
      {message && <Alert severity="warning">{message}</Alert>}
      <Typography variant="body2" color="text.secondary">{partner.advice}</Typography>
      {partner.grants.map((item) => (
        <Typography key={`${item.direction}-${item.created_at}-${item.job_id}`} variant="body2">{grantLine(item)}</Typography>
      ))}
      {partner.preflights.map((item) => (
        <Typography key={`${item.direction}-${item.created_at}-${item.job_id}`} variant="body2">{preflightLine(item)}</Typography>
      ))}
    </Box>
  );
}

// Sent details: a fax to a partner you take part with — why it waits, what the partner's preflight said (never a
// receipt) and which window its call used.
export function SentAdmission({ client, jobId }: { client: Pick<AdminAPIClient, 'admissionFax'>; jobId: string }) {
  const [sentences, setSentences] = useState<string[]>([]);
  useEffect(() => {
    let live = true;
    client.admissionFax(jobId).then((found) => { if (live) setSentences(found.sentences ?? []); })
      .catch(() => { if (live) setSentences([]); });
    return () => { live = false; };
  }, [client, jobId]);
  if (sentences.length === 0) return null;
  return (
    <Alert severity="info" sx={{ mt: 2 }} data-testid="sent-admission">
      {sentences.map((sentence) => <Typography key={sentence} variant="body2">{sentence}</Typography>)}
    </Alert>
  );
}

export default function PartnerAdmission({ client, canWrite }: { client: Client; canWrite: boolean }) {
  const [view, setView] = useState<View | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lines, setLines] = useState('');
  const [reserve, setReserve] = useState('1');
  const [windowMinutes, setWindowMinutes] = useState('10');
  const [urgent, setUrgent] = useState<'go' | 'wait'>('go');
  const [deadline, setDeadline] = useState<'go' | 'wait'>('go');

  const show = useCallback((found: View) => {
    setView(found);
    setLines(found.settings.lines === null ? '' : String(found.settings.lines));
    setReserve(String(found.settings.reserve));
    setWindowMinutes(String(found.settings.window_minutes));
    setUrgent(found.settings.urgent);
    setDeadline(found.settings.deadline);
  }, []);
  useEffect(() => {
    client.partnerAdmission().then(show)
      .catch((failure) => setError(problem(failure, 'Line windows could not be loaded. Try again.')));
  }, [client, show]);

  const save = async () => {
    try {
      show(await client.saveAdmissionSettings({ lines: lines.trim() === '' ? null : Number(lines),
        reserve: Number(reserve), window_minutes: Number(windowMinutes), urgent, deadline }));
      setError(null);
    } catch (failure) {
      setError(problem(failure, 'This could not be saved. Try again.'));
    }
  };

  if (!view) return error ? <Alert severity="warning" sx={{ mt: 4 }}>{error}</Alert> : null;
  return (
    <Card variant="outlined" sx={{ mt: 4, borderRadius: 2 }} data-testid="partner-admission">
      <CardContent>
        <Typography variant="h6" component="h2">Line windows and preflight</Typography>
        <Typography variant="body2">{view.lines_sentence}</Typography>
        <Typography variant="body2">{view.policy_sentence}</Typography>
        <Typography variant="body2">{view.legacy.sentence}</Typography>
        <Typography variant="caption" color="text.secondary" display="block">{view.not_a_receipt}</Typography>
        {error && <Alert severity="warning" sx={{ mt: 1 }}>{error}</Alert>}
        {canWrite && (
          <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} sx={{ mt: 1 }} flexWrap="wrap">
            <TextField size="small" label="Lines for partners" value={lines} placeholder="From your trunk"
              onChange={(event) => setLines(event.target.value)} inputProps={{ 'data-testid': 'admission-lines' }} />
            <TextField size="small" label="Lines kept for other callers" value={reserve}
              onChange={(event) => setReserve(event.target.value)} />
            <TextField size="small" label="Window, minutes" value={windowMinutes}
              onChange={(event) => setWindowMinutes(event.target.value)} />
            <TextField select size="small" label="Urgent faxes" value={urgent}
              onChange={(event) => setUrgent(event.target.value as 'go' | 'wait')}>
              <MenuItem value="go">Start without a window</MenuItem>
              <MenuItem value="wait">Wait for a window</MenuItem>
            </TextField>
            <TextField select size="small" label="A fax that would miss its send-by time" value={deadline}
              onChange={(event) => setDeadline(event.target.value as 'go' | 'wait')}>
              <MenuItem value="go">Starts without a window</MenuItem>
              <MenuItem value="wait">Still waits</MenuItem>
            </TextField>
            <Button variant="outlined" onClick={() => void save()}>Save</Button>
          </Stack>
        )}
        {view.partners.length === 0 && (
          <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>Add a partner first.</Typography>
        )}
        {view.partners.map((partner) => (
          <PartnerSection key={partner.peer_id} client={client} partner={partner} canWrite={canWrite} onSaved={show} />
        ))}
      </CardContent>
    </Card>
  );
}
