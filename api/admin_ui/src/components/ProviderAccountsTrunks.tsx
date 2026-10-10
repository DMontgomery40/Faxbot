// Several trunks: the trunk page edits one trunk account at a time, so with more than one it starts with a
// picker. Savings & optimization → Prices & plans shows a card's origin-rated rows: what a call costs from each site or country
// to each number prefix, with where and when the price was read.
import { useEffect, useState } from 'react';
import {
  FormControl, InputLabel, Link, MenuItem, Paper, Select, Table, TableBody, TableCell, TableHead, TableRow, Typography,
} from '@mui/material';
import { formatLocalDate } from '../api/time';
import { isForbidden } from '../api/client';
import { DeliveryError, formatRate } from './delivery/shared';
import type { ProviderAccount, RulesApi } from './ProviderRulesApi';

export function TrunkPicker({ api, value, onChange }: {
  api: RulesApi;
  // The trunk account being edited; null is the first trunk.
  value: string | null;
  onChange: (key: string) => void;
}) {
  const [trunks, setTrunks] = useState<ProviderAccount[]>([]);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    let live = true;
    api.accounts().then((state) => { if (live) setTrunks(state.accounts.filter((account) => account.provider === 'sip')); })
      .catch((failure) => {
        // Someone who may not see provider accounts edits the first trunk; any other failure is said.
        if (live && !isForbidden(failure)) setError(failure);
      });
    return () => { live = false; };
  }, [api]);
  if (error) return <DeliveryError error={error} onClose={() => setError(null)} />;
  if (trunks.length < 2) return null;
  const selected = value ?? trunks.find((trunk) => trunk.primary)?.key ?? trunks[0].key;
  return (
    <FormControl size="small" sx={{ minWidth: 280, mb: 2 }}>
      <InputLabel id="trunk-picker">Trunk</InputLabel>
      <Select labelId="trunk-picker" label="Trunk" value={selected} inputProps={{ 'aria-label': 'Trunk' }}
        onChange={(event) => onChange(event.target.value)}>
        {trunks.map((trunk) => <MenuItem key={trunk.key} value={trunk.key}>{trunk.label}</MenuItem>)}
      </Select>
    </FormControl>
  );
}

// One origin-rated row of a rate card (design §3.7).
export interface OriginRateRow {
  // Where calls start, in words: "Leeds office", "United Kingdom" or "Anywhere".
  origin_label: string;
  // The row's origin as stored ('any', a site key, 'country:GB') and whether it is a carrier's published price.
  origin?: string;
  published?: boolean;
  destination_prefix: string;
  currency: string;
  per_minute: string;
  per_page: string;
  per_call: string;
  billing_increment_seconds: number;
  minimum_seconds: number;
  source_url: string | null;
  captured_on: string | null;
}

function priceText(row: OriginRateRow): string {
  const parts = [formatRate(row.per_minute, row.currency, 'minute'), formatRate(row.per_page, row.currency, 'page'),
    formatRate(row.per_call, row.currency, 'call')].filter(Boolean);
  return parts.join(', ') || 'No charge';
}

function billingText(row: OriginRateRow): string {
  const increment = row.billing_increment_seconds === 60 ? 'whole minutes' : `${row.billing_increment_seconds}-second steps`;
  return row.minimum_seconds > 0 ? `${increment}, at least ${row.minimum_seconds} seconds` : increment;
}

export function OriginRates({ rows, cardLabel }: { rows: OriginRateRow[]; cardLabel: string }) {
  if (rows.length === 0) return null;
  return (
    <Paper variant="outlined" sx={{ borderRadius: 2, overflowX: 'auto', mt: 1 }}>
      <Typography variant="subtitle2" sx={{ px: 2, pt: 1.5 }}>{cardLabel}: prices by where calls start</Typography>
      <Typography variant="caption" color="text.secondary" sx={{ px: 2, display: 'block' }}>
        Faxbot uses the row for the account's site with the longest matching number prefix, and the card's own price
        when no row matches.
      </Typography>
      <Table size="small" aria-label={`${cardLabel} prices by where calls start`}>
        <TableHead>
          <TableRow><TableCell>Calls from</TableCell><TableCell>To numbers starting with</TableCell><TableCell>Price</TableCell>
            <TableCell>Billed in</TableCell><TableCell>Source</TableCell></TableRow>
        </TableHead>
        <TableBody>
          {rows.map((row) => (
            <TableRow key={`${row.origin_label}-${row.destination_prefix}`}>
              <TableCell>{row.origin_label}</TableCell>
              <TableCell>{row.destination_prefix}</TableCell>
              <TableCell>{priceText(row)}</TableCell>
              <TableCell>{billingText(row)}</TableCell>
              <TableCell>
                {row.source_url ? <Link href={row.source_url} target="_blank" rel="noreferrer">Published price</Link> : 'Entered here'}
                {row.captured_on && `, read on ${formatLocalDate(row.captured_on)}`}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </Paper>
  );
}
