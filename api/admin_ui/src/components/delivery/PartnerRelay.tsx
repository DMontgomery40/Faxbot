// Partners → a partner → Relay: a partner sends your faxes as local calls in its country, or you send theirs.
// Off until both sides sign: the relay offers (where, how much, when), the sender accepts, either side ends it.
import { useCallback, useEffect, useState } from 'react';
import {
  Box, Button, Checkbox, CircularProgress, Dialog, DialogActions, DialogContent, DialogTitle, Divider,
  FormControlLabel, Paper, Stack, Switch, TextField, Typography,
} from '@mui/material';
import AdminAPIClient from '../../api/client';
import type { DirectPartner, RelayAgreement } from '../../api/deliveryTypes';
import { StatusChip } from '../access/AccessViews';
import { DeliveryError, Notice } from './shared';

const STATE: Record<RelayAgreement['state'], { label: string; tone: 'success' | 'warning' | 'default' | 'info' }> = {
  offered: { label: 'Offered', tone: 'info' },
  accepting: { label: 'Accepting', tone: 'warning' },
  active: { label: 'In force', tone: 'success' },
  withdrawn: { label: 'Ended', tone: 'default' },
};

const WEEKDAYS = ['mon', 'tue', 'wed', 'thu', 'fri'];

// Shown before the offer is made, so the administrator reads it before anything is signed.
export function relayPrivacy(partner: string) {
  return `You will see what ${partner}'s faxes contain. If they are another organization, check that your agreement `
    + 'with them covers this; Faxbot does not make relaying exempt from any privacy rules.';
}

export function relayResale(partner: string) {
  return `Because ${partner} is another organization, your carrier may count it as your customer: Telnyx's terms `
    + 'make you responsible for your end users and what they send. Faxbot has not checked the terms of SignalWire, '
    + 'Sinch or Phaxio.';
}

function countries(text: string) {
  return text.split(/[\s,]+/).map((code) => code.trim().toUpperCase()).filter(Boolean);
}

function Agreement({ item, canWrite, busy, onAccept, onWithdraw, onPrices }: {
  item: RelayAgreement; canWrite: boolean; busy: boolean;
  onAccept: () => void; onWithdraw: () => void; onPrices: () => void;
}) {
  const state = STATE[item.state];
  return (
    <Paper variant="outlined" sx={{ p: 2, borderRadius: 2 }} data-testid="relay-agreement">
      <Box display="flex" gap={1} alignItems="center" flexWrap="wrap" mb={1}>
        <StatusChip label={state.label} tone={state.tone} />
        <Typography variant="subtitle1">{item.summary}</Typography>
      </Box>
      <Typography variant="body2">{item.status}</Typography>
      {item.notes.map((note) => (
        <Typography key={note} variant="body2" color="text.secondary" sx={{ mt: 1 }}>{note}</Typography>
      ))}
      {item.price && item.price.routes.length > 0 && (
        <Box mt={1}>
          <Typography variant="body2" fontWeight={600}>Their prices</Typography>
          {item.price.routes.map((route) => (
            <Typography key={`${route.country}-${route.kind}`} variant="body2" color="text.secondary">{route.text}</Typography>
          ))}
        </Box>
      )}
      {canWrite && item.state !== 'withdrawn' && (
        <Box display="flex" gap={1} mt={1.5} flexWrap="wrap">
          {item.role === 'sender' && item.state === 'offered' && (
            <Button size="small" variant="contained" onClick={onAccept} disabled={busy}>Accept</Button>
          )}
          {item.role === 'relay' && (
            <Button size="small" onClick={onPrices} disabled={busy}>Send new prices</Button>
          )}
          <Button size="small" color="error" onClick={onWithdraw} disabled={busy}>End agreement</Button>
        </Box>
      )}
    </Paper>
  );
}

export default function PartnerRelay({ client, partner, canWrite, open, onClose }: {
  client: AdminAPIClient;
  partner: DirectPartner;
  canWrite: boolean;
  open: boolean;
  onClose: () => void;
}) {
  const name = partner.organization;
  const [items, setItems] = useState<RelayAgreement[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [offering, setOffering] = useState(false);
  const [accepting, setAccepting] = useState<RelayAgreement | null>(null);
  // The offer form.
  const [places, setPlaces] = useState('');
  const [pages, setPages] = useState('');
  const [spend, setSpend] = useState('');
  const [currency, setCurrency] = useState('USD');
  const [limitHours, setLimitHours] = useState(false);
  const [from, setFrom] = useState('08:00');
  const [until, setUntil] = useState('18:00');
  const [together, setTogether] = useState(false);
  const [sameOrganization, setSameOrganization] = useState(false);
  // The acceptance form.
  const [replyNumber, setReplyNumber] = useState('');
  const [shareCalls, setShareCalls] = useState(false);
  const [ownOffice, setOwnOffice] = useState(false);
  const [marketing, setMarketing] = useState(false);
  const [businessNumber, setBusinessNumber] = useState('');
  const [contact, setContact] = useState('');
  const [optOut, setOptOut] = useState('');
  const [quoteCountry, setQuoteCountry] = useState('');

  const load = useCallback(async () => {
    try {
      setItems((await client.listRelayAgreements(partner.id)).agreements);
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

  const relaying = (items ?? []).some((item) => item.role === 'relay' && item.state !== 'withdrawn');
  const verified = partner.state === 'verified';

  const offer = async () => {
    const ok = await run(async () => (await client.offerRelay({
      partner: partner.id, countries: countries(places),
      monthly_pages: pages.trim() ? Number(pages) : null,
      monthly_spend: spend.trim() ? { amount: spend.trim(), currency: currency.trim().toUpperCase() } : null,
      hours: limitHours ? { days: WEEKDAYS, from, until } : null,
      together, same_organization: sameOrganization,
    })).detail);
    if (ok) setOffering(false);
  };

  const accept = async () => {
    if (!accepting) return;
    const ok = await run(async () => (await client.acceptRelay(accepting.id, {
      reply_number: replyNumber.trim() || null, together: shareCalls, same_organization: ownOffice,
      marketing: marketing ? { business_number: businessNumber, contact, opt_out: optOut } : null,
    })).detail);
    if (ok) setAccepting(null);
  };

  const withdraw = (item: RelayAgreement) => void run(async () => (await client.withdrawRelay(item.id)).detail);
  const prices = (item: RelayAgreement) => void run(async () => (await client.refreshRelayPrice(item.id)).detail);
  const quote = () => void run(async () => (await client.askRelayQuote(partner.id, countries(quoteCountry))).detail);

  return (
    <Dialog open={open} onClose={onClose} fullWidth maxWidth="md" aria-labelledby="relay-title">
      <DialogTitle id="relay-title">Relay with {name}</DialogTitle>
      <DialogContent>
        <Typography variant="body2" color="text.secondary" mb={2}>
          A partner can send your faxes as local calls in its own country, and you can send theirs. Nothing is
          relayed until both of you agree, because the partner that sends a fax sees what it contains.
        </Typography>
        <Notice message={notice} onClose={() => setNotice(null)} />
        <DeliveryError error={error} onClose={() => setError(null)} />
        {items === null ? <CircularProgress size={24} /> : (
          <Stack spacing={2}>
            {items.length === 0 && (
              <Typography variant="body2" data-testid="relay-none">No relay agreement with {name} yet.</Typography>
            )}
            {items.map((item) => (
              <Agreement key={item.id} item={item} canWrite={canWrite} busy={busy}
                onAccept={() => { setShareCalls(item.together); setAccepting(item); }}
                onWithdraw={() => withdraw(item)} onPrices={() => prices(item)} />
            ))}
          </Stack>
        )}

        {accepting && (
          <Box mt={3} data-testid="relay-accept">
            <Divider sx={{ mb: 2 }} />
            <Typography variant="subtitle1" gutterBottom>Accept: {accepting.summary}</Typography>
            <TextField fullWidth size="small" label="Number for replies" value={replyNumber}
              onChange={(event) => setReplyNumber(event.target.value)}
              helperText="Printed at the top of every page they send for you. Leave empty to use your reply number." />
            {accepting.together && (
              <FormControlLabel control={<Switch checked={shareCalls} onChange={(e) => setShareCalls(e.target.checked)} />}
                label="Our faxes may share a call with other organizations' faxes to the same number" />
            )}
            <FormControlLabel control={<Switch checked={ownOffice} onChange={(e) => setOwnOffice(e.target.checked)} />}
              label={`${name} is part of our own organization`} />
            <FormControlLabel control={<Checkbox checked={marketing} onChange={(e) => setMarketing(e.target.checked)} />}
              label="These faxes are marketing" />
            {marketing && (
              <Stack spacing={1.5} mt={1}>
                <Typography variant="body2" color="text.secondary">
                  Australia asks marketing faxes to name your business number, how to reach you and how to stop
                  them; Faxbot prints these on the first page.
                </Typography>
                <TextField size="small" label="Business number, such as an ABN" value={businessNumber}
                  onChange={(e) => setBusinessNumber(e.target.value)} />
                <TextField size="small" label="Contact details" value={contact} onChange={(e) => setContact(e.target.value)} />
                <TextField size="small" label="Where to ask to stop these faxes" value={optOut}
                  onChange={(e) => setOptOut(e.target.value)} />
              </Stack>
            )}
            <Box display="flex" gap={1} mt={2}>
              <Button variant="contained" onClick={() => void accept()} disabled={busy}>Accept the offer</Button>
              <Button onClick={() => setAccepting(null)} disabled={busy}>Cancel</Button>
            </Box>
          </Box>
        )}

        {canWrite && verified && !relaying && !offering && items !== null && (
          <Box mt={3}>
            <Button variant="outlined" onClick={() => setOffering(true)} sx={{ borderRadius: 2 }}>
              Offer to send {name}'s faxes
            </Button>
          </Box>
        )}
        {offering && (
          <Box mt={3} data-testid="relay-offer">
            <Divider sx={{ mb: 2 }} />
            <Typography variant="subtitle1" gutterBottom>Let {name} send faxes through you</Typography>
            <Stack spacing={1.5}>
              <TextField size="small" label="Countries, such as AU" value={places} onChange={(e) => setPlaces(e.target.value)}
                helperText="The countries they may send faxes to through you, by two-letter code." />
              <TextField size="small" label="Most pages a month" value={pages} onChange={(e) => setPages(e.target.value)}
                inputProps={{ inputMode: 'numeric' }} helperText="Leave empty for no limit." />
              <Box display="flex" gap={1}>
                <TextField size="small" label="Most to spend a month" value={spend} onChange={(e) => setSpend(e.target.value)}
                  helperText="Leave empty for no limit." />
                <TextField size="small" label="Currency" value={currency} onChange={(e) => setCurrency(e.target.value)}
                  sx={{ width: 110 }} />
              </Box>
              <FormControlLabel control={<Switch checked={limitHours} onChange={(e) => setLimitHours(e.target.checked)} />}
                label="Only on weekdays, during these hours" />
              {limitHours && (
                <Box display="flex" gap={1}>
                  <TextField size="small" label="From" value={from} onChange={(e) => setFrom(e.target.value)} sx={{ width: 120 }} />
                  <TextField size="small" label="Until" value={until} onChange={(e) => setUntil(e.target.value)} sx={{ width: 120 }} />
                </Box>
              )}
              <FormControlLabel control={<Switch checked={together} onChange={(e) => setTogether(e.target.checked)} />}
                label="Their faxes may share a call with other senders' faxes to the same number" />
              <FormControlLabel control={<Switch checked={sameOrganization} onChange={(e) => setSameOrganization(e.target.checked)} />}
                label={`${name} is part of our own organization`} />
              <Typography variant="body2" color="text.secondary">{relayPrivacy(name)}</Typography>
              {!sameOrganization && (
                <Typography variant="body2" color="text.secondary" data-testid="relay-resale">{relayResale(name)}</Typography>
              )}
            </Stack>
            <Box display="flex" gap={1} mt={2}>
              <Button variant="contained" onClick={() => void offer()} disabled={busy || countries(places).length === 0}>
                Make the offer
              </Button>
              <Button onClick={() => setOffering(false)} disabled={busy}>Cancel</Button>
            </Box>
          </Box>
        )}

        {canWrite && verified && (
          <Box mt={3} display="flex" gap={1} alignItems="center" flexWrap="wrap">
            <TextField size="small" label="Country to price" value={quoteCountry} onChange={(e) => setQuoteCountry(e.target.value)}
              sx={{ width: 180 }} />
            <Button size="small" onClick={quote} disabled={busy || countries(quoteCountry).length === 0}>
              Ask {name} for prices
            </Button>
          </Box>
        )}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Close</Button>
      </DialogActions>
    </Dialog>
  );
}
