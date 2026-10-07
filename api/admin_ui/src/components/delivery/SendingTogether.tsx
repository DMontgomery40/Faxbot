// Sending short faxes to the same number together in one call: the per-number
// setting in a fax number's Details, and a fax's waiting or shared-call lines in Jobs.
import { useEffect, useState } from 'react';
import {
  Alert, Box, Button, Checkbox, Divider, FormControl, FormControlLabel, FormLabel, ListItem, ListItemText, Radio,
  RadioGroup, Stack, Switch, TextField, Typography,
} from '@mui/material';
import AdminAPIClient from '../../api/client';
import type { BatchingNumber, Boundaries, FaxTogether, FaxTogetherSummary } from '../../api/batchingTypes';
import { DeliveryError } from './shared';
import { formatServerTime } from '../../api/time';

// "10:40 PM" today, or "Oct 4, 10:40 PM" on another day, in the viewer's time zone.
export function formatWaitTime(value: string | undefined | null, now: Date = new Date()): string {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  const time = date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  if (date.toDateString() === now.toDateString()) return time;
  return `${date.toLocaleDateString([], { month: 'short', day: 'numeric' })}, ${time}`;
}

// One line for Jobs: a waiting fax says until when; a fax that went with others says with how many.
export function togetherLine(together: FaxTogetherSummary | null | undefined): string | null {
  if (!together) return null;
  if (together.state === 'waiting') {
    if (together.send_now) return 'Going now with the faxes waiting for this number.';
    const until = formatWaitTime(together.waiting_until);
    return `Waiting to go with other faxes to this number${until ? ` until ${until}` : ''}.`;
  }
  if (together.state === 'together' && together.others !== undefined) {
    return `Sent in one call with ${together.others === 1 ? '1 other fax' : `${together.others} other faxes`}.`;
  }
  return null;
}

// The Jobs detail lines: waiting with "Send now", or the shared call, its reference and this fax's share.
export function FaxTogetherItem({ client, jobId, together, onChanged }: {
  client: AdminAPIClient;
  jobId: string;
  together: FaxTogetherSummary | null | undefined;
  onChanged?: () => void;
}) {
  const [detail, setDetail] = useState<FaxTogether | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let live = true;
    setDetail(null);
    setError(null);
    if (!together) return undefined;
    client.getFaxTogether(jobId).then((value) => { if (live) setDetail(value); }).catch(() => undefined);
    return () => { live = false; };
  }, [client, jobId, together]);

  if (!together) return null;
  const current: FaxTogetherSummary = { ...together, ...(detail ?? {}) } as FaxTogetherSummary;
  const sendNow = async () => {
    setBusy(true);
    setError(null);
    try {
      setDetail(await client.sendWaitingFaxNow(jobId));
      onChanged?.();
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };
  const line = togetherLine(current);
  return (
    <>
      <Divider />
      <ListItem>
        <ListItemText
          primary="Sending together"
          secondary={
            <Stack component="span" spacing={0.5} sx={{ display: 'flex' }}>
              <span>{line ?? 'Sent on its own.'}</span>
              {current.state === 'together' && (
                <span data-testid="together-layout">
                  {detail?.layout_sentence
                    ?? `Its separator page says ${current.reference} (document ${current.document_number} of ${current.documents}).`}
                </span>
              )}
              {detail?.share && <span>{detail.share.sentence}</span>}
            </Stack>
          }
        />
        {current.state === 'waiting' && !current.send_now && (
          <Button size="small" variant="outlined" disabled={busy} onClick={() => void sendNow()} sx={{ ml: 1 }}>
            Send now
          </Button>
        )}
      </ListItem>
      {error !== null && <Box px={2}><DeliveryError error={error} onClose={() => setError(null)} /></Box>}
    </>
  );
}

// The per-number setting, shown in a fax number's Details. It saves on its own.
export function SendingTogetherPanel({ client, number, canWrite }: {
  client: AdminAPIClient;
  number: string;
  canWrite: boolean;
}) {
  const [view, setView] = useState<BatchingNumber | null>(null);
  const [enabled, setEnabled] = useState(false);
  const [agreed, setAgreed] = useState(false);
  const [wait, setWait] = useState('10');
  const [pages, setPages] = useState('30');
  const [mixed, setMixed] = useState(false);
  const [boundaries, setBoundaries] = useState<Boundaries>('separators');
  const [marksAgreed, setMarksAgreed] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState<string | null>(null);

  const show = (loaded: BatchingNumber) => {
    setView(loaded);
    setEnabled(loaded.enabled);
    setAgreed(false);
    setWait(String(loaded.max_wait_minutes));
    setPages(String(loaded.max_pages));
    setMixed(loaded.mixed_senders);
    setBoundaries(loaded.boundaries ?? 'separators');
    setMarksAgreed(false);
  };

  useEffect(() => {
    let live = true;
    setView(null);
    setError(null);
    setSaved(null);
    client.getBatching(number).then((loaded) => { if (live) show(loaded); }).catch((failure) => { if (live) setError(failure); });
    return () => { live = false; };
  }, [client, number]);

  if (!view) return error ? <DeliveryError error={error} onClose={() => setError(null)} /> : null;

  const turningOn = enabled && !view.enabled;
  const waitMinutes = Number(wait);
  const maxPages = Number(pages);
  const valid = Number.isInteger(waitMinutes) && waitMinutes >= 1 && waitMinutes <= 60
    && Number.isInteger(maxPages) && maxPages >= 2 && maxPages <= 200;
  // How each document is marked: separator pages need no agreement; anything else needs the recipient's.
  const currentMarks = view.boundaries ?? 'separators';
  const marksChanged = enabled && boundaries !== currentMarks;
  const marksChoice = view.boundaries_choices?.find((choice) => choice.value === boundaries);
  const marksNeedAgreement = marksChanged && Boolean(marksChoice?.agreement_text);
  const changed = enabled !== view.enabled || (enabled && (waitMinutes !== view.max_wait_minutes
    || maxPages !== view.max_pages || mixed !== view.mixed_senders || marksChanged));

  const save = async () => {
    setBusy(true);
    setError(null);
    setSaved(null);
    try {
      const result = enabled
        ? await client.saveBatching(number, {
          enabled: true, recipient_agreed: agreed, max_wait_minutes: waitMinutes, max_pages: maxPages,
          mixed_senders: mixed, version: view.version,
          ...(marksChanged ? { boundaries, boundaries_agreed: marksNeedAgreement && marksAgreed } : {}),
        })
        : await client.turnOffBatching(number);
      show(result);
      setSaved(result.state_sentence);
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Box mt={2} data-testid="sending-together">
      <Typography variant="subtitle2">Sending together</Typography>
      <Typography variant="body2" sx={{ mt: 0.5 }}>{view.state_sentence}</Typography>
      <Typography variant="body2" color="text.secondary">{view.route_sentence}</Typography>
      {view.agreement && (
        <Typography variant="body2" color="text.secondary">
          The recipient's agreement was recorded by {view.agreement.by} on {formatServerTime(view.agreement.at)}.
        </Typography>
      )}
      <Typography variant="body2" color="text.secondary">{view.savings.sentence}</Typography>
      {view.boundaries_sentence && (
        <Typography variant="body2" color="text.secondary" data-testid="boundaries-state">{view.boundaries_sentence}</Typography>
      )}
      {view.boundaries_agreement && (
        <Typography variant="body2" color="text.secondary">
          The recipient's agreement to this was recorded by {view.boundaries_agreement.by} on {formatServerTime(view.boundaries_agreement.at)}.
        </Typography>
      )}
      {Boolean(view.savings.separator_pages?.calls) && (
        <Typography variant="body2" color="text.secondary">{view.savings.separator_pages?.sentence}</Typography>
      )}
      <DeliveryError error={error} onClose={() => setError(null)} />
      {saved && <Alert severity="success" sx={{ mt: 1 }} onClose={() => setSaved(null)}>{saved}</Alert>}
      <FormControlLabel sx={{ mt: 1 }} disabled={!canWrite || busy}
        control={<Switch checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />}
        label="Send short faxes to this number together in one call" />
      {turningOn && (
        <FormControlLabel disabled={!canWrite || busy}
          control={<Checkbox checked={agreed} onChange={(e) => setAgreed(e.target.checked)} />}
          label={view.agreement_text} />
      )}
      {enabled && (
        <Box display="flex" gap={2} flexWrap="wrap" mt={1}>
          <TextField size="small" type="number" label="Longest wait (minutes)" value={wait}
            onChange={(e) => setWait(e.target.value)} disabled={!canWrite || busy}
            inputProps={{ min: 1, max: 60 }} sx={{ width: 200 }} />
          <TextField size="small" type="number" label="Most pages in one call" value={pages}
            onChange={(e) => setPages(e.target.value)} disabled={!canWrite || busy}
            helperText="Separator pages count too." inputProps={{ min: 2, max: 200 }} sx={{ width: 200 }} />
          <FormControlLabel disabled={!canWrite || busy}
            control={<Switch checked={mixed} onChange={(e) => setMixed(e.target.checked)} />}
            label="Faxes from different senders may share a call" />
        </Box>
      )}
      {enabled && view.boundaries_choices && (
        <FormControl sx={{ mt: 1 }} disabled={!canWrite || busy}>
          <FormLabel id={`boundaries-${number}`}>How each document is marked</FormLabel>
          <RadioGroup aria-labelledby={`boundaries-${number}`} value={boundaries}
            onChange={(e) => { setBoundaries(e.target.value as Boundaries); setMarksAgreed(false); }}>
            {view.boundaries_choices.map((choice) => (
              <FormControlLabel key={choice.value} value={choice.value} control={<Radio size="small" />} label={choice.label} />
            ))}
          </RadioGroup>
          {view.boundaries_keeps && (
            <Typography variant="body2" color="text.secondary">{view.boundaries_keeps}</Typography>
          )}
          {marksNeedAgreement && marksChoice?.agreement_text && (
            <FormControlLabel
              control={<Checkbox checked={marksAgreed} onChange={(e) => setMarksAgreed(e.target.checked)} />}
              label={marksChoice.agreement_text} />
          )}
        </FormControl>
      )}
      {canWrite && (
        <Box mt={1}>
          <Button size="small" variant="outlined" onClick={() => void save()}
            disabled={busy || !changed || (enabled && !valid) || (turningOn && !agreed)
              || (marksNeedAgreement && !marksAgreed)}>
            {enabled ? 'Save sending together' : 'Turn off sending together'}
          </Button>
        </Box>
      )}
    </Box>
  );
}
