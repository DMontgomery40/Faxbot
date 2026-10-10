// Delivery setup → Providers & accounts → Where Faxbot may dial: the classes of numbers and the countries Faxbot may call,
// each with why, and the price a minute above which a fax waits for approval. A fax to anything not
// allowed waits in Sent for approval; nothing is dialed (routing/guard.py).
import { useCallback, useEffect, useState } from 'react';
import {
  Box, Button, Dialog, DialogActions, DialogContent, DialogTitle, Paper, Stack, Table, TableBody, TableCell,
  TableHead, TableRow, TextField, Typography,
} from '@mui/material';
import { isForbidden } from '../api/client';
import { DeliveryError, Notice } from './delivery/shared';
import { formatServerTime } from '../api/time';
import type { DialingChoice, DialingClass, DialingState, RulesApi } from './ProviderRulesApi';

// The state to keep when only the ceiling changes: the administrator's own choice, else Faxbot's default.
function keptChoice(item: DialingClass): DialingChoice {
  if (!item.chosen) return 'default';
  return item.allowed ? 'allowed' : 'blocked';
}

function why(item: DialingClass): string {
  if (!item.first_delivered_at) return item.sentence;
  return `${item.sentence} First delivered ${formatServerTime(item.first_delivered_at)}.`;
}

function Rows({ items, canWrite, saving, onChange, onCeiling }: {
  items: DialingClass[]; canWrite: boolean; saving: boolean;
  onChange: (item: DialingClass, choice: DialingChoice) => void; onCeiling: (item: DialingClass) => void;
}) {
  return (
    <TableBody>
      {items.map((item) => (
        <TableRow key={item.key}>
          <TableCell>{item.label}</TableCell>
          <TableCell>{item.allowed ? 'Yes' : 'No'}</TableCell>
          <TableCell>{why(item)}</TableCell>
          <TableCell>{item.ceiling ? item.ceiling.text : 'No ceiling'}</TableCell>
          <TableCell align="right" sx={{ whiteSpace: 'nowrap' }}>
            {canWrite && item.changeable && (
              <>
                {!item.allowed && <Button size="small" disabled={saving} onClick={() => onChange(item, 'allowed')}>Allow</Button>}
                {item.allowed && <Button size="small" disabled={saving} onClick={() => onChange(item, 'blocked')}>Block</Button>}
                {item.chosen && <Button size="small" disabled={saving} onClick={() => onChange(item, 'default')}>Use the default</Button>}
                <Button size="small" disabled={saving} onClick={() => onCeiling(item)}>Price ceiling</Button>
              </>
            )}
          </TableCell>
        </TableRow>
      ))}
    </TableBody>
  );
}

function Head() {
  return (
    <TableHead>
      <TableRow>
        <TableCell>Numbers</TableCell><TableCell>May dial</TableCell><TableCell>Why</TableCell>
        <TableCell>Price ceiling</TableCell><TableCell />
      </TableRow>
    </TableHead>
  );
}

export default function DialDestinations({ api, canWrite }: { api: RulesApi; canWrite: boolean }) {
  const [state, setState] = useState<DialingState | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [hidden, setHidden] = useState(false);
  const [saving, setSaving] = useState(false);
  const [country, setCountry] = useState('');
  const [ceilingFor, setCeilingFor] = useState<DialingClass | null>(null);
  const [ceiling, setCeiling] = useState('');

  const load = useCallback(() => {
    api.dialing().then(setState).catch((failure) => {
      if (isForbidden(failure)) setHidden(true);
      else setError(failure);
    });
  }, [api]);
  useEffect(() => { load(); }, [load]);

  const change = async (key: string, choice: DialingChoice, newCeiling?: string) => {
    setSaving(true);
    setError(null);
    try {
      const result = await api.changeDialing(key, choice, newCeiling);
      setState(result);
      setNotice(result.sentence ?? null);
      return true;
    } catch (failure) {
      setError(failure);
      return false;
    } finally {
      setSaving(false);
    }
  };

  if (hidden) return null;
  if (!state) return <DeliveryError error={error} />;
  const openCeiling = (item: DialingClass) => { setCeilingFor(item); setCeiling(item.ceiling?.amount ?? ''); };

  return (
    <Box sx={{ mb: 3 }} aria-label="Where Faxbot may dial" role="region">
      <Typography variant="h6" component="h2">Where Faxbot may dial</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
        Faxbot calls only the numbers allowed here. A fax to anything else waits in Sent for your approval, and nothing is dialed.
      </Typography>
      <Notice message={notice} onClose={() => setNotice(null)} />
      <DeliveryError error={error} onClose={() => setError(null)} />
      <Paper variant="outlined" sx={{ borderRadius: 2, overflowX: 'auto', mb: 2 }}>
        <Table size="small" aria-label="Kinds of numbers">
          <Head />
          <Rows items={state.classes} canWrite={canWrite} saving={saving}
            onChange={(item, choice) => void change(item.key, choice)} onCeiling={openCeiling} />
        </Table>
      </Paper>
      <Typography variant="subtitle2" component="h3">Countries</Typography>
      {state.countries.length > 0 ? (
        <Paper variant="outlined" sx={{ borderRadius: 2, overflowX: 'auto', mb: 1 }}>
          <Table size="small" aria-label="Countries">
            <Head />
            <Rows items={state.countries} canWrite={canWrite} saving={saving}
              onChange={(item, choice) => void change(item.key, choice)} onCeiling={openCeiling} />
          </Table>
        </Paper>
      ) : (
        <Typography variant="body2" sx={{ mb: 1 }}>No other country yet.</Typography>
      )}
      {state.prefixes.map((item) => (
        <Typography key={item.prefix} variant="body2">
          Numbers starting {item.prefix} are allowed: the routing rule ‘{item.rule}’ names them.
        </Typography>
      ))}
      <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>{state.other_countries}</Typography>
      {canWrite && (
        <Stack direction="row" spacing={1} alignItems="center" sx={{ mt: 1 }}>
          <TextField size="small" label="Country" placeholder="GB or +44" value={country}
            onChange={(event) => setCountry(event.target.value)} inputProps={{ 'aria-label': 'Country to allow' }} />
          <Button disabled={saving || !country.trim()}
            onClick={() => void change(country.trim(), 'allowed').then((done) => { if (done) setCountry(''); })}>
            Allow this country
          </Button>
        </Stack>
      )}
      <Dialog open={ceilingFor !== null} onClose={() => setCeilingFor(null)} fullWidth maxWidth="xs"
        aria-labelledby="dialing-ceiling-title">
        <DialogTitle id="dialing-ceiling-title">Price ceiling: {ceilingFor?.label}</DialogTitle>
        <DialogContent>
          <Typography variant="body2" sx={{ mb: 2 }}>
            A fax whose cheapest call costs more than this a minute waits for your approval. Leave it empty for no ceiling.
          </Typography>
          <TextField fullWidth size="small" label="Highest price a minute" placeholder="0.25" value={ceiling}
            onChange={(event) => setCeiling(event.target.value)} />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setCeilingFor(null)}>Cancel</Button>
          <Button variant="contained" disabled={saving}
            onClick={() => ceilingFor && void change(ceilingFor.key, keptChoice(ceilingFor), ceiling.trim())
              .then((done) => { if (done) setCeilingFor(null); })}>Save</Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
