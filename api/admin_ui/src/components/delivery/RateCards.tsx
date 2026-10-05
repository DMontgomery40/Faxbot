// Advertised provider prices Faxbot uses to estimate cost and rank routes.
import { useEffect, useState } from 'react';
import {
  Alert, Box, Button, Card, CardContent, Link, Paper, Stack, Table, TableBody, TableCell, TableContainer, TableHead,
  TableRow, TextField, Typography,
} from '@mui/material';
import PriceChangeIcon from '@mui/icons-material/PriceChange';
import AdminAPIClient from '../../api/client';
import type { PublishedPlans, RateCard } from '../../api/deliveryTypes';
import { ConfirmDialog, EmptyState, Field, FormDialog, useSmallScreens } from '../access/AccessViews';
import { DeliveryError, formatMoney, formatRate } from './shared';
import { providerLabel } from '../../providerLabels';

const BILLING = [
  { value: 1, label: 'Per second' },
  { value: 6, label: 'Every 6 seconds' },
  { value: 60, label: 'Whole minutes' },
];

const PROVIDERS = ['sip', 'signalwire', 'phaxio', 'sinch', 'documo', 'humblefax', 'efax']
  .map((value) => ({ value, label: providerLabel(value) }));

// Today in the viewer's own time zone, as YYYY-MM-DD (not the UTC date).
export function localToday(now: Date = new Date()): string {
  const pad = (value: number) => String(value).padStart(2, '0');
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

// A monthly fee with nothing charged per minute, page or call.
function flatPlan(card: RateCard): boolean {
  return Number(card.monthly_fee ?? 0) > 0 && !(Number(card.per_minute) > 0) && !(Number(card.per_page) > 0)
    && !(Number(card.per_call) > 0);
}

function billingText(card: RateCard): string {
  if (card.included_in_plan || flatPlan(card)) return 'Flat monthly fee';
  return `${billingLabel(card.billing_increment_seconds)}${card.minimum_seconds ? `, at least ${card.minimum_seconds} seconds` : ''}`;
}

function billingLabel(seconds: number): string {
  return BILLING.find((option) => option.value === seconds)?.label ?? `Every ${seconds} seconds`;
}

function pricing(card: RateCard): string {
  const monthly = card.monthly_fee && Number(card.monthly_fee) > 0
    ? `${formatMoney({ amount: card.monthly_fee, currency: card.currency })} a month` : null;
  if (card.included_in_plan && monthly) return `${monthly}, faxes included`;
  const parts = [formatRate(card.per_minute, card.currency, 'minute'), formatRate(card.per_page, card.currency, 'page'),
    formatRate(card.per_call, card.currency, 'call'), monthly && `plus ${monthly}`].filter(Boolean);
  return parts.length ? parts.join(', ') : 'No charge';
}

const EMPTY: RateCard = {
  provider_id: 'sip', label: '', direction: 'outbound', currency: 'USD', per_minute: '0', per_page: '0', per_call: '0',
  billing_increment_seconds: 60, minimum_seconds: 0, source_url: null, captured_on: '',
  monthly_fee: null,
};

function CardDialog({ card, onClose, onSave, busy, error }: {
  card: RateCard | null;
  onClose: () => void;
  onSave: (card: RateCard) => void;
  busy: boolean;
  error: unknown;
}) {
  const [draft, setDraft] = useState<RateCard>(() => {
    const start = card ?? EMPTY;
    return start.captured_on ? start : { ...start, captured_on: localToday() };
  });
  const set = <K extends keyof RateCard>(key: K, value: RateCard[K]) => setDraft((current) => ({ ...current, [key]: value }));
  return (
    <FormDialog open={card !== null} title={card?.id ? 'Edit rate card' : 'Add rate card'} submitLabel="Save" busy={busy}
      error={null} canSubmit={Boolean(draft.label.trim() && draft.provider_id && draft.captured_on)}
      onSubmit={() => onSave(draft)} onClose={onClose}>
      <DeliveryError error={error} />
      <TextField select fullWidth margin="normal" label="Provider" value={draft.provider_id}
        onChange={(e) => set('provider_id', e.target.value)} SelectProps={{ native: true }} InputLabelProps={{ shrink: true }}>
        {PROVIDERS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
      </TextField>
      <Field label="Name" value={draft.label} onChange={(value) => set('label', value)} helperText="For example, Telnyx SIP trunk." />
      <TextField select fullWidth margin="normal" label="Used for" value={draft.direction}
        onChange={(e) => set('direction', e.target.value as RateCard['direction'])} SelectProps={{ native: true }} InputLabelProps={{ shrink: true }}>
        <option value="outbound">Sending</option>
        <option value="inbound">Receiving</option>
      </TextField>
      <Box display="grid" gridTemplateColumns={{ xs: '1fr', sm: '1fr 1fr 1fr' }} columnGap={2}>
        <Field label={`Per minute (${draft.currency})`} value={draft.per_minute} onChange={(value) => set('per_minute', value)} />
        <Field label={`Per page (${draft.currency})`} value={draft.per_page} onChange={(value) => set('per_page', value)} />
        <Field label={`Per call (${draft.currency})`} value={draft.per_call} onChange={(value) => set('per_call', value)} />
      </Box>
      <Field label={`Monthly plan fee (${draft.currency})`} value={draft.monthly_fee ?? ''}
        onChange={(value) => set('monthly_fee', value.trim() ? value : null)}
        helperText="For an unlimited plan, enter the monthly fee and leave the other prices at 0." />
      {flatPlan(draft) ? (
        // Only a monthly fee: there is no call time to round, so billing rules do not apply.
        <Field label="Currency" value={draft.currency} onChange={(value) => set('currency', value.toUpperCase().slice(0, 3))} />
      ) : (
        <Box display="grid" gridTemplateColumns={{ xs: '1fr', sm: '1fr 1fr 1fr' }} columnGap={2}>
          <TextField select fullWidth margin="normal" label="Billing" value={String(draft.billing_increment_seconds)}
            onChange={(e) => set('billing_increment_seconds', Number(e.target.value))} SelectProps={{ native: true }} InputLabelProps={{ shrink: true }}>
            {BILLING.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
          </TextField>
          <Field label="Minimum seconds" value={String(draft.minimum_seconds)} onChange={(value) => set('minimum_seconds', Number(value) || 0)} />
          <Field label="Currency" value={draft.currency} onChange={(value) => set('currency', value.toUpperCase().slice(0, 3))} />
        </Box>
      )}
      <Field label="Price source" value={draft.source_url ?? ''} onChange={(value) => set('source_url', value || null)}
        helperText="The provider's pricing page." />
      <Field label="Advertised on" type="date" value={draft.captured_on} onChange={(value) => set('captured_on', value)} />
    </FormDialog>
  );
}

// Sending providers in use with no sending card, such as eFax, whose API is priced by quote: one
// sentence on what the provider publishes, and its plan as an estimate on request.
export function usePublishedPlans(client: AdminAPIClient, cards: RateCard[]): PublishedPlans[] {
  const [found, setFound] = useState<PublishedPlans[]>([]);
  const key = cards.filter((card) => card.direction === 'outbound').map((card) => card.provider_id).sort().join(',');
  useEffect(() => {
    let current = true;
    client.getPublishedPlansInUse().then((result) => { if (current) setFound(result.items ?? []); })
      .catch(() => { if (current) setFound([]); });
    return () => { current = false; };
  }, [client, key]);
  return found;
}

export default function RateCards({ client, cards, canWrite, onChanged }: {
  client: AdminAPIClient;
  cards: RateCard[];
  canWrite: boolean;
  onChanged: () => void;
}) {
  const { isMobile } = useSmallScreens();
  const published = usePublishedPlans(client, cards);
  const [editing, setEditing] = useState<RateCard | null>(null);
  const [removing, setRemoving] = useState<RateCard | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const write = async (next: RateCard[]) => {
    setBusy(true);
    setError(null);
    try {
      await client.saveRateCards(next);
      setEditing(null);
      setRemoving(null);
      onChanged();
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };

  const save = (draft: RateCard) => {
    const others = cards.filter((card) => card.id !== draft.id
      && !(card.provider_id === draft.provider_id && card.direction === draft.direction));
    void write([...others, draft]);
  };

  const actions = (card: RateCard) => canWrite && (
    <>
      <Button size="small" onClick={() => { setError(null); setEditing(card); }} aria-label={`Edit ${card.label}`}>Edit</Button>
      <Button size="small" color="error" onClick={() => { setError(null); setRemoving(card); }} aria-label={`Remove ${card.label}`}>Remove</Button>
    </>
  );

  const source = (card: RateCard) => (
    <Typography variant="caption" color="text.secondary">
      Advertised on {card.captured_on}{card.source_url ? <> · <Link href={card.source_url} target="_blank" rel="noreferrer">source</Link></> : null}
    </Typography>
  );

  return (
    <Box>
      {published.map((item) => (
        <Alert key={item.provider_id} severity="info" sx={{ mb: 2 }} data-testid={`published-plans-${item.provider_id}`}
          action={canWrite && item.card ? (
            <Button color="inherit" size="small" onClick={() => { setError(null); setEditing({ ...item.card!, id: null }); }}>
              Use a published plan as my estimate
            </Button>
          ) : undefined}>
          {item.sentence}
          {item.page_url && <> <Link href={item.page_url} target="_blank" rel="noreferrer">{item.page_label}</Link></>}
        </Alert>
      ))}
      {canWrite && (
        <Box mb={2}>
          <Button variant="outlined" onClick={() => { setError(null); setEditing({ ...EMPTY }); }} sx={{ borderRadius: 2 }}>Add rate card</Button>
        </Box>
      )}
      {cards.length === 0 ? (
        <EmptyState icon={<PriceChangeIcon />} title="No rate cards"
          text="Add your providers' advertised prices so Faxbot can estimate costs and pick the cheapest route." />
      ) : isMobile ? (
        <Stack spacing={2}>
          {cards.map((card) => (
            <Card key={card.id ?? `${card.provider_id}-${card.direction}`} variant="outlined" sx={{ borderRadius: 2 }}>
              <CardContent>
                <Typography variant="subtitle1">{card.label}</Typography>
                <Typography variant="body2">{pricing(card)}</Typography>
                <Typography variant="body2" color="text.secondary">
                  {card.direction === 'outbound' ? 'Sending' : 'Receiving'} · {billingText(card)}
                </Typography>
                {source(card)}
                <Box mt={1}>{actions(card)}</Box>
              </CardContent>
            </Card>
          ))}
        </Stack>
      ) : (
        <TableContainer component={Paper} sx={{ borderRadius: 2 }}>
          <Table>
            <TableHead>
              <TableRow>
                <TableCell>Name</TableCell>
                <TableCell>Used for</TableCell>
                <TableCell>Price</TableCell>
                <TableCell>Billing</TableCell>
                <TableCell>Source</TableCell>
                <TableCell align="right">Actions</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {cards.map((card) => (
                <TableRow key={card.id ?? `${card.provider_id}-${card.direction}`} hover>
                  <TableCell>{card.label}</TableCell>
                  <TableCell>{card.direction === 'outbound' ? 'Sending' : 'Receiving'}</TableCell>
                  <TableCell>{pricing(card)}</TableCell>
                  <TableCell>{billingText(card)}</TableCell>
                  <TableCell>{source(card)}</TableCell>
                  <TableCell align="right">{actions(card)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableContainer>
      )}
      {editing && <CardDialog card={editing} busy={busy} error={error} onClose={() => setEditing(null)} onSave={save} />}
      <ConfirmDialog open={removing !== null} title="Remove this rate card?" danger busy={busy} error={error}
        text="Faxbot stops estimating costs for this provider until you add a new card."
        confirmLabel="Remove" onConfirm={() => void write(cards.filter((card) => card !== removing))}
        onCancel={() => setRemoving(null)} />
    </Box>
  );
}
