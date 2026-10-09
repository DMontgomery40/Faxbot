// Costs → Prices & plans: enter your own prices by where calls start for one sending card, such as your Leeds
// office's contract rate to UK numbers. Saving replaces the rows you entered; earlier ones are kept as history.
// Shipped published rows (a carrier's own price list) are not changed here.
import { useEffect, useState } from 'react';
import {
  Alert, Button, Dialog, DialogActions, DialogContent, DialogTitle, MenuItem, Stack, TextField, Typography,
} from '@mui/material';
import AdminAPIClient from '../../api/client';
import type { OriginRateRow } from '../ProviderAccountsTrunks';
import { rulesApiFor } from '../ProviderRulesApi';
import LoadFailed, { saysFailure } from '../common/LoadFailed';

export interface EnteredRow {
  origin: string;
  destination_prefix: string;
  per_minute: string;
  per_page: string;
  per_call: string;
  billing_increment_seconds: number;
  minimum_seconds: number;
}

const EMPTY_ROW: EnteredRow = { origin: 'any', destination_prefix: '', per_minute: '0', per_page: '0', per_call: '0',
  billing_increment_seconds: 60, minimum_seconds: 0 };

// A row the card already lists, as it is saved back: only rows you entered (published rows stay as shipped).
export function enteredRows(rows: OriginRateRow[]): EnteredRow[] {
  return rows.filter((row) => row.published === false).map((row) => ({
    origin: row.origin ?? 'any',
    destination_prefix: row.destination_prefix.startsWith('+') ? row.destination_prefix.slice(1) : '',
    per_minute: row.per_minute, per_page: row.per_page, per_call: row.per_call,
    billing_increment_seconds: row.billing_increment_seconds, minimum_seconds: row.minimum_seconds,
  }));
}

export default function OriginRateRows({ client, route, label, existing, onSaved }: {
  client: AdminAPIClient;
  route: string;
  label: string;
  // The rows the card lists now; the ones you entered are kept when you add one.
  existing: OriginRateRow[];
  onSaved: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [sites, setSites] = useState<Array<{ key: string; name: string }>>([]);
  // Your sites could not be read: prices can still be entered for anywhere or a country, and the failure is said.
  const [sitesUnread, setSitesUnread] = useState(false);
  useEffect(() => {
    if (!open) return undefined;
    let live = true;
    setSitesUnread(false);
    rulesApiFor(client).accounts().then((state) => { if (live) setSites(state.sites ?? []); })
      .catch((failure) => { if (live && saysFailure(failure)) setSitesUnread(true); });
    return () => { live = false; };
  }, [client, open]);
  const [row, setRow] = useState<EnteredRow>(EMPTY_ROW);
  const [country, setCountry] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const set = (change: Partial<EnteredRow>) => setRow((current) => ({ ...current, ...change }));

  const save = async () => {
    setBusy(true);
    const origin = row.origin === 'country' ? `country:${country.trim().toUpperCase()}` : row.origin;
    try {
      await client.call({ method: 'PUT', path: `/routing/rate-cards/${encodeURIComponent(route)}/rows`,
        body: { rows: [...enteredRows(existing), { ...row, origin }] } });
      setOpen(false);
      setRow(EMPTY_ROW);
      setError(null);
      onSaved();
    } catch (failure) {
      setError((failure as { detail?: string })?.detail ?? 'Faxbot could not save this price.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <Button size="small" onClick={() => setOpen(true)}>Add a price by where calls start</Button>
      <Dialog open={open} onClose={() => setOpen(false)} fullWidth maxWidth="sm">
        <DialogTitle>{label}: a price by where calls start</DialogTitle>
        <DialogContent>
          <Stack spacing={2} sx={{ mt: 1 }}>
            <Typography variant="body2" color="text.secondary">
              Faxbot uses the row for the account's site, then its country, then anywhere, with the longest matching
              number prefix. Prices are in this card's currency.
            </Typography>
            {error && <Alert severity="warning">{error}</Alert>}
            <TextField select size="small" label="Calls from" value={row.origin} onChange={(event) => set({ origin: event.target.value })}>
              <MenuItem value="any">Anywhere</MenuItem>
              <MenuItem value="country">A country</MenuItem>
              {sites.map((site) => <MenuItem key={site.key} value={site.key}>{site.name}</MenuItem>)}
            </TextField>
            {sitesUnread && <LoadFailed testId="origin-sites-unread"
              text="Your sites could not be loaded, so only Anywhere and A country are offered. Try again." />}
            {row.origin === 'country' && (
              <TextField size="small" label="Country code" placeholder="GB" value={country}
                onChange={(event) => setCountry(event.target.value)} />
            )}
            <TextField size="small" label="To numbers starting with" placeholder="+44113" value={row.destination_prefix}
              onChange={(event) => set({ destination_prefix: event.target.value })} />
            <Stack direction="row" spacing={1}>
              <TextField size="small" label="A minute" value={row.per_minute} onChange={(event) => set({ per_minute: event.target.value })} />
              <TextField size="small" label="A page" value={row.per_page} onChange={(event) => set({ per_page: event.target.value })} />
              <TextField size="small" label="A call" value={row.per_call} onChange={(event) => set({ per_call: event.target.value })} />
            </Stack>
            <Stack direction="row" spacing={1}>
              <TextField size="small" type="number" label="Billed in steps of (seconds)" value={row.billing_increment_seconds}
                onChange={(event) => set({ billing_increment_seconds: Number(event.target.value) })} />
              <TextField size="small" type="number" label="At least (seconds)" value={row.minimum_seconds}
                onChange={(event) => set({ minimum_seconds: Number(event.target.value) })} />
            </Stack>
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setOpen(false)}>Cancel</Button>
          <Button variant="contained" disabled={busy || !row.destination_prefix.trim()} onClick={() => void save()}>Save</Button>
        </DialogActions>
      </Dialog>
    </>
  );
}
