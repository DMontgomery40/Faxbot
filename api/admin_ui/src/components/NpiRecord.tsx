// Numbers → Your NPI record: your NPIs (one per location) and the numbers the NPI registry (NPPES) lists for each,
// with when Faxbot read them, so advice never suggests giving up a number still printed there.
import { useCallback, useEffect, useState } from 'react';
import { Alert, Box, Button, Paper, Stack, Table, TableBody, TableCell, TableRow, TextField, Typography } from '@mui/material';
import AdminAPIClient from '../api/client';
import type { NpiRecord } from '../api/numberAdviceTypes';
import { parseServerTime } from '../api/time';
import { DeliveryError } from './delivery/shared';

function readDay(value: string | null): string {
  return parseServerTime(value ?? '')?.toLocaleDateString(undefined, { day: 'numeric', month: 'long', year: 'numeric' }) ?? '';
}

export default function NpiRecordPanel({ client, canWrite }: { client: AdminAPIClient; canWrite: boolean }) {
  const [record, setRecord] = useState<NpiRecord | null>(null);
  const [npi, setNpi] = useState('');
  const [label, setLabel] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    let live = true;
    client.call<NpiRecord>({ method: 'GET', path: '/routing/npi' })
      .then((found) => { if (live) setRecord(found); }).catch((problem) => { if (live) setError(problem); });
    return () => { live = false; };
  }, [client]);
  const run = useCallback(async (request: { method: string; path: string; body?: unknown }) => {
    setBusy(true);
    setError(null);
    try {
      setRecord(await client.call<NpiRecord>(request));
      return true;
    } catch (problem) {
      setError(problem);
      return false;
    } finally {
      setBusy(false);
    }
  }, [client]);
  if (!record) return error ? <DeliveryError error={error} /> : null;
  return (
    <Paper variant="outlined" sx={{ p: 2, borderRadius: 2 }} data-testid="npi-record">
      <Typography variant="h6" component="h3">Your NPI record</Typography>
      <Typography variant="body2" sx={{ mb: 1 }}>{record.sentence}</Typography>
      {record.problem && <Alert severity="warning" sx={{ mb: 1 }}>{record.problem}</Alert>}
      {error ? <DeliveryError error={error} onClose={() => setError(null)} /> : null}
      {record.npis.map((item) => (
        <Box key={item.npi} sx={{ mb: 1.5 }}>
          <Typography variant="body2" sx={{ fontWeight: 600 }}>
            {`NPI ${item.npi}${item.label ? ` (${item.label})` : ''}${item.name ? `: ${item.name}` : ''}`}
          </Typography>
          {item.read_at && <Typography variant="caption" color="text.secondary">{`Read ${readDay(item.read_at)}`}</Typography>}
          {item.numbers.length > 0 && (
            <Table size="small" aria-label={`Numbers on NPI ${item.npi}`}>
              <TableBody>
                {item.numbers.map((number) => (
                  <TableRow key={`${number.number}-${number.kind}-${number.where}`}>
                    <TableCell>{number.display}</TableCell>
                    <TableCell>{number.kind === 'fax' ? 'Fax' : 'Phone'}</TableCell>
                    <TableCell>{number.where}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
          {canWrite && (
            <Button size="small" disabled={busy} sx={{ mt: 0.5 }}
              onClick={() => void run({ method: 'DELETE', path: `/routing/npi/${encodeURIComponent(item.npi)}` })}>
              Remove this NPI
            </Button>
          )}
        </Box>
      ))}
      {canWrite && (
        <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} sx={{ mt: 1 }} alignItems={{ sm: 'center' }}>
          <TextField size="small" label="Your NPI" value={npi} onChange={(event) => setNpi(event.target.value.trim())}
            inputProps={{ inputMode: 'numeric', maxLength: 10, 'data-testid': 'npi-input' }} />
          <TextField size="small" label="Location (optional)" value={label} placeholder="Denver office"
            onChange={(event) => setLabel(event.target.value)} />
          <Button variant="outlined" disabled={busy || npi.length !== 10}
            onClick={() => void run({ method: 'POST', path: '/routing/npi', body: { npi, label } })
              .then((done) => { if (done) { setNpi(''); setLabel(''); } })}>
            Add NPI
          </Button>
          {record.npis.length > 0 && (
            <Button disabled={busy} onClick={() => void run({ method: 'POST', path: '/routing/npi/check' })}>
              Check NPPES now
            </Button>
          )}
        </Stack>
      )}
    </Paper>
  );
}

