// A sent fax Faxbot could not confirm: who owns it, the checks cheapest first, and settling it.
// Nothing here sends anything by itself: the receipt query and "send it again" go only when the person selects them.
import { useCallback, useEffect, useState } from 'react';
import {
  Alert, Box, Button, Checkbox, Chip, Divider, FormControl, FormControlLabel, InputLabel, Link, List, ListItem, ListItemText,
  MenuItem, Radio, RadioGroup, Select, Stack, TextField, Typography,
} from '@mui/material';
import type AdminAPIClient from '../../api/client';
import type {
  CertaintyCheck, CertaintyEvent, CertaintyForFax, CertaintyItem, CertaintyOutcome, CertaintyPerson,
} from '../../api/certaintyTypes';
import { formatServerTime } from '../../api/time';
import { deliveryErrorMessage } from '../delivery/shared';
import { ContinuationFacts, onTheirWay } from './SentContinuation';
import { shortTime } from './text';

export const RESULT_LABELS: Record<string, string> = {
  delivered: 'Delivered (signed)',
  not_delivered: 'Not delivered (signed)',
  partial: 'Partly delivered (signed)',
  probably_not_delivered: 'Probably not delivered',
  probably_partial: 'Probably only partly delivered',
  consistent: 'Fits a delivered fax',
  asking: 'Asking',
  unavailable: 'Not available',
  unknown: "Can't tell yet",
  not_done: 'Not done yet',
  sent: 'Sent',
};

export const OUTCOME_LABELS: Record<CertaintyOutcome, string> = {
  delivered: 'Delivered',
  not_delivered: 'Not delivered',
  unknown: "Can't tell",
};

// The server writes each state as one sentence; a time in it is shown here in local time.
export function certaintyStateSentence(item: Pick<CertaintyItem, 'state_key' | 'state_text' | 'due_at' | 'owner'>): string {
  if (item.state_key === 'assigned' && item.due_at) {
    return `Assigned to ${item.owner?.name || 'someone'}; settle by ${shortTime(item.due_at)}.`;
  }
  return item.state_text;
}

function resultColor(check: CertaintyCheck): 'success' | 'error' | 'warning' | 'default' | 'info' {
  if (check.result === 'delivered' || check.result === 'consistent') return 'success';
  if (check.result === 'not_delivered') return 'error';
  if (check.result.startsWith('probably') || check.result === 'partial') return 'warning';
  if (check.result === 'asking' || check.result === 'sent') return 'info';
  return 'default';
}

function CheckRow({ check, index, item, busy, onDraft, onSendQuery }: {
  check: CertaintyCheck; index: number; item: CertaintyItem; busy: boolean;
  onDraft: () => void; onSendQuery: () => void;
}) {
  const open = item.state === 'open';
  return (
    <ListItem alignItems="flex-start" disableGutters sx={{ display: 'block' }}>
      <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
        <Typography variant="subtitle2">{index + 1}. {check.title}</Typography>
        <Chip size="small" variant="outlined" label={check.cost} />
        <Chip size="small" color={resultColor(check)} label={RESULT_LABELS[check.result] ?? 'Not done yet'} />
      </Stack>
      <Typography variant="body2" sx={{ mt: 0.5 }}>{check.text}</Typography>
      {check.meaning && <Typography variant="body2" color="text.secondary">{check.meaning}</Typography>}
      {check.kind === 'npi_lookup' && check.source_url && (
        <Link variant="body2" href={check.source_url} target="_blank" rel="noopener noreferrer">The NPI registry</Link>
      )}
      {open && check.kind === 'phone_call' && check.script && (
        <Box component="ol" sx={{ pl: 3, my: 1 }}>
          {check.script.map((line) => <li key={line}><Typography variant="body2">{line}</Typography></li>)}
        </Box>
      )}
      {open && check.kind === 'receipt_query' && item.actions.includes('send_query') && (
        <Stack direction="row" spacing={1} sx={{ mt: 1 }}>
          <Button size="small" onClick={onDraft} disabled={busy}>Check the page</Button>
          <Button size="small" variant="outlined" onClick={onSendQuery} disabled={busy}>Fax it to the recipient</Button>
        </Stack>
      )}
    </ListItem>
  );
}

function SettleForm({ item, busy, onSettle }: {
  item: CertaintyItem; busy: boolean;
  onSettle: (outcome: CertaintyOutcome, reason: string, sendAgain: boolean, sendRest: boolean) => void;
}) {
  const [outcome, setOutcome] = useState<CertaintyOutcome | ''>(item.suggestion ?? '');
  const [reason, setReason] = useState('');
  const [sendAgain, setSendAgain] = useState(false);
  // Only the pages the receiving machine did not confirm (routing/continuation.py), instead of the whole fax.
  const [sendRest, setSendRest] = useState(false);
  const rest = item.continuation?.state === 'offered' && item.continuation.may_send ? item.continuation : null;
  const ready = outcome !== '' && reason.trim().length >= 3;
  const notDelivered = outcome === 'not_delivered';
  return (
    <Box component="form" onSubmit={(event) => { event.preventDefault(); if (ready) onSettle(outcome as CertaintyOutcome, reason, sendAgain && notDelivered, sendRest && notDelivered && rest !== null); }}>
      <Typography variant="subtitle1" component="h3" gutterBottom>Settle it</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
        Faxbot never sends this fax again on its own. Choose what happened; Faxbot keeps your name and your reason with it.
      </Typography>
      <RadioGroup row value={outcome} onChange={(event) => setOutcome(event.target.value as CertaintyOutcome)}>
        {(Object.keys(OUTCOME_LABELS) as CertaintyOutcome[]).map((value) => (
          <FormControlLabel key={value} value={value} control={<Radio />} label={OUTCOME_LABELS[value]} disabled={busy} />
        ))}
      </RadioGroup>
      {outcome === 'not_delivered' && !item.moved_on && (
        <FormControlLabel control={<Checkbox checked={sendAgain} onChange={(event) => { setSendAgain(event.target.checked); if (event.target.checked) setSendRest(false); }} disabled={busy} />}
          label="Send it again now, as a new fax linked to this one" />
      )}
      {notDelivered && !item.moved_on && rest && (
        <Box>
          <FormControlLabel control={<Checkbox checked={sendRest} onChange={(event) => { setSendRest(event.target.checked); if (event.target.checked) setSendAgain(false); }} disabled={busy} />}
            label={`${rest.action} only, as a new fax linked to this one`} />
          <Box sx={{ pl: 4 }}>
            <ContinuationFacts basis={rest.basis} costText={rest.cost_text} warning={rest.warning} />
          </Box>
        </Box>
      )}
      {notDelivered && !item.moved_on && item.continuation?.state === 'unavailable' && (
        <Typography variant="body2" color="text.secondary">{item.continuation.reason}</Typography>
      )}
      <TextField fullWidth size="small" label="How do you know?" value={reason} disabled={busy} sx={{ my: 1 }}
        onChange={(event) => setReason(event.target.value)} inputProps={{ maxLength: 400 }}
        helperText="For example, who you spoke to, or what the recipient faxed back." />
      <Button type="submit" variant="contained" disabled={busy || !ready}>Settle</Button>
    </Box>
  );
}

function ItemPanel({ client, initial, onOpenFax }: { client: AdminAPIClient; initial: CertaintyItem; onOpenFax?: (faxId: string) => void }) {
  const [item, setItem] = useState(initial);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [people, setPeople] = useState<CertaintyPerson[] | null>(null);
  const [events, setEvents] = useState<CertaintyEvent[] | null>(null);

  useEffect(() => { setItem(initial); }, [initial]);

  const act = async (operation: () => Promise<CertaintyItem | void>, done?: string) => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const next = await operation();
      if (next) setItem(next);
      if (done) setNotice(done);
      if (events) setEvents((await client.uncertainHistory(item.id)).events);
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };

  const draft = () => act(async () => {
    const blob = await client.receiptQueryPdf(item.id);
    const url = URL.createObjectURL(blob);
    window.open(url, '_blank', 'noopener');
    setTimeout(() => URL.revokeObjectURL(url), 60_000);
  });
  const sendQuery = () => act(() => client.sendReceiptQuery(item.id, item.version),
    'Receipt query sent. When the recipient faxes it back, settle this fax.');
  const settle = (outcome: CertaintyOutcome, reason: string, sendAgain: boolean, sendRest: boolean) => {
    const rest = item.continuation?.state === 'offered' ? item.continuation : null;
    if (sendRest && rest) {
      // Sending only the remaining pages settles the item as not delivered, with the new fax linked to it.
      return act(async () => (await client.sendContinuation(item.fax_id, {
        first_page: rest.first_page, reason, version: item.version })).item ?? undefined,
      `Settled. ${onTheirWay(rest.pages_text)}`);
    }
    return act(
      () => client.settleUncertain(item.id, { outcome, reason, version: item.version, send_again: sendAgain }),
      sendAgain ? 'Settled. The fax is on its way again as a new fax.' : 'Settled.');
  };
  const loadPeople = async () => {
    if (people) return;
    try {
      setPeople((await client.uncertainAssignees(item.id)).people);
    } catch (failure) {
      setError(failure);
    }
  };
  const assign = (principalId: string) => act(() => client.assignUncertain(item.id, principalId, item.version));
  const reload = () => act(() => client.getUncertain(item.id));
  const showHistory = async () => {
    try {
      setEvents((await client.uncertainHistory(item.id)).events);
    } catch (failure) {
      setError(failure);
    }
  };

  const settled = item.state === 'settled';
  return (
    <Box sx={{ mb: 2 }}>
      <Alert severity={settled ? (item.outcome === 'delivered' ? 'success' : 'info') : (item.overdue ? 'error' : 'warning')} sx={{ mb: 2 }}>
        <Typography variant="body2" fontWeight={600}>{certaintyStateSentence(item)}</Typography>
        <Typography variant="body2">{item.why}</Typography>
        {settled && item.settled_by && item.settled_reason && <Typography variant="body2">“{item.settled_reason}”{item.settled_at ? `, ${formatServerTime(item.settled_at)}` : ''}</Typography>}
      </Alert>
      {item.moved_on && <Alert severity="info" sx={{ mb: 2 }}>{item.moved_on.text}</Alert>}
      {notice && <Alert severity="success" sx={{ mb: 2 }} onClose={() => setNotice(null)}>{notice}</Alert>}
      {error ? <Alert severity="error" sx={{ mb: 2 }} onClose={() => setError(null)}>{deliveryErrorMessage(error)}</Alert> : null}
      <Typography variant="body2" sx={{ mb: 1 }}>
        Reference {item.reference}{item.owner ? ` · Owner: ${item.owner.name ?? 'someone who is no longer listed'}` : ''}
        {item.owner && item.owner_source_text ? ` (${item.owner_source_text})` : ''}
      </Typography>
      {!settled && item.actions.includes('assign') && (
        <FormControl size="small" sx={{ minWidth: 240, mb: 1 }}>
          <InputLabel id={`owner-${item.id}`}>Give it to</InputLabel>
          <Select labelId={`owner-${item.id}`} label="Give it to" value="" onOpen={() => void loadPeople()} disabled={busy}
            onChange={(event) => { const next = String(event.target.value); if (next) void assign(next); }}>
            {(people ?? []).map((person) => <MenuItem key={person.id} value={person.id}>{person.name}</MenuItem>)}
            {people && people.length === 0 && <MenuItem disabled value="">Nobody else can see this fax.</MenuItem>}
          </Select>
        </FormControl>
      )}
      {item.checks && (
        <>
          <Typography variant="subtitle1" component="h3" sx={{ mt: 1 }}>Ways to find out, cheapest first</Typography>
          <List dense>
            {item.checks.map((check, index) => (
              <CheckRow key={check.kind} check={check} index={index} item={item} busy={busy} onDraft={() => void draft()}
                onSendQuery={() => void sendQuery()} />
            ))}
          </List>
        </>
      )}
      {!settled && item.suggestion && (
        <Alert severity="info" sx={{ mb: 2 }}>The checks point to “{OUTCOME_LABELS[item.suggestion]}”. You decide.</Alert>
      )}
      {!settled && item.actions.includes('settle') && <SettleForm key={item.version} item={item} busy={busy} onSettle={(o, r, s, rest) => void settle(o, r, s, rest)} />}
      {settled && item.resend_fax_id && onOpenFax && (
        <Button onClick={() => onOpenFax(item.resend_fax_id as string)}>Open the new fax</Button>
      )}
      <Stack direction="row" spacing={1} sx={{ mt: 1 }}>
        <Button size="small" onClick={() => void showHistory()} disabled={busy}>Show history</Button>
        <Button size="small" onClick={() => void reload()} disabled={busy}>Reload</Button>
      </Stack>
      {events && (
        <List dense>
          {events.map((event, index) => (
            <ListItem key={`${event.at}-${index}`} disableGutters>
              <ListItemText primary={event.text} secondary={formatServerTime(event.at)} />
            </ListItem>
          ))}
        </List>
      )}
    </Box>
  );
}

// The Sent details section for one fax; nothing shows for a fax Faxbot is sure of.
export function FaxCertaintyItem({ client, jobId, onOpenFax }: { client: AdminAPIClient; jobId: string; onOpenFax?: (faxId: string) => void }) {
  const [found, setFound] = useState<CertaintyForFax | null>(null);
  const load = useCallback(() => {
    let current = true;
    client.uncertainForFax(jobId).then((next) => { if (current) setFound(next); }).catch(() => { if (current) setFound(null); });
    return () => { current = false; };
  }, [client, jobId]);
  useEffect(() => load(), [load]);
  if (!found || (found.items.length === 0 && !found.about)) return null;
  return (
    <>
      <Divider />
      <Box sx={{ px: 2, py: 2 }}>
        <Typography variant="h6" component="h2" gutterBottom>What happened to this fax?</Typography>
        {found.about && (
          <Alert severity="info" action={onOpenFax ? <Button onClick={() => onOpenFax(found.about!.fax_id)}>Open it</Button> : undefined}>
            {found.about.kind === 'resend'
              ? 'This fax was sent again because an earlier fax did not arrive.'
              : 'This one-page fax asks the recipient whether an earlier fax arrived.'}
          </Alert>
        )}
        {found.items.map((item) => <ItemPanel key={item.id} client={client} initial={item} onOpenFax={onOpenFax} />)}
      </Box>
    </>
  );
}

export default FaxCertaintyItem;
