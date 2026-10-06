// What fax calls on the phone line negotiated: speed, compression and error correction, as each fax engine
// reported them. Measurement only; nothing here changes how faxes are sent. The server words every sentence
// and label, so the console and `faxbot providers trunk negotiation` say the same thing.
import { useCallback, useEffect, useState } from 'react';
import {
  Box,
  Card,
  CardContent,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  ToggleButton,
  ToggleButtonGroup,
  Typography,
  useMediaQuery,
  useTheme,
} from '@mui/material';
import type AdminAPIClient from '../api/client';
import { isForbidden } from '../api/client';
import type { NegotiationSummary as Summary } from '../api/sipTypes';

const DAYS = [7, 30, 90] as const;

export function secondsPerPage(value: number | null): string {
  return value === null ? '—' : `${value.toLocaleString(undefined, { maximumFractionDigits: 1 })} s`;
}

export function callsPerFax(value: number | null): string {
  return value === null ? '—' : value.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

// Faxes → Received, one fax's details: how its call went. Shows nothing when no phone-line call carried the
// fax, or when the person may not read the phone line's records.
export function ReceivedCallNegotiation({ client, faxId }: { client: AdminAPIClient; faxId: string | null | undefined }) {
  const [sentence, setSentence] = useState<string | null>(null);
  useEffect(() => {
    let current = true;
    setSentence(null);
    if (!faxId) return undefined;
    client.getReceivedNegotiation(faxId)
      .then((view) => { if (current) setSentence(view.sentence); })
      .catch(() => { /* No call on the phone line carried this fax, or no access: nothing to show. */ });
    return () => { current = false; };
  }, [client, faxId]);
  if (!sentence) return null;
  return (
    <Box sx={{ mb: 1.5 }} data-testid="received-call-negotiation">
      <Typography variant="caption" color="text.secondary">How the call went</Typography>
      <Typography variant="body2">{sentence}</Typography>
    </Box>
  );
}

// Providers → the phone line's page: calls per compression, error correction and speed.
export default function NegotiationSummary({ client }: { client: AdminAPIClient }) {
  const theme = useTheme();
  const narrow = useMediaQuery(theme.breakpoints.down('md'));
  const [days, setDays] = useState<number>(30);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const load = useCallback(async (period: number) => {
    try {
      setSummary(await client.getNegotiationSummary(period));
      setNote(null);
    } catch (error) {
      setSummary(null);
      setNote(isForbidden(error) ? 'You do not have access to call measurements.'
        : 'Call measurements could not be loaded. Try again.');
    }
  }, [client]);

  useEffect(() => { void load(days); }, [days, load]);

  const groups = summary?.groups ?? [];
  return (
    <Box data-testid="negotiation-summary" sx={{ mt: 2 }}>
      <Typography variant="subtitle1">How fax calls went</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
        Speed, compression and error correction of answered fax calls, as your fax engines reported them.
      </Typography>
      <ToggleButtonGroup exclusive size="small" value={days} aria-label="Period"
        onChange={(_, next: number | null) => next && setDays(next)} sx={{ mb: 1 }}>
        {DAYS.map((value) => (
          <ToggleButton key={value} value={value} sx={{ textTransform: 'none' }}>Last {value} days</ToggleButton>
        ))}
      </ToggleButtonGroup>
      {note && <Typography variant="body2" color="text.secondary">{note}</Typography>}
      {summary && <Typography variant="body2" data-testid="negotiation-measured">{summary.sentence}</Typography>}
      {groups.length > 0 && (narrow ? (
        <Stack spacing={1} sx={{ mt: 1 }}>
          {groups.map((group) => (
            <Card key={`${group.coding_label}|${group.speed_label}`} variant="outlined">
              <CardContent>
                <Typography variant="body2" fontWeight={600}>{group.coding_label}</Typography>
                <Typography variant="body2">Speed: {group.speed_label}</Typography>
                <Typography variant="body2">
                  {group.calls} {group.calls === 1 ? 'call' : 'calls'}, {group.success_percent}% succeeded,
                  {' '}{secondsPerPage(group.seconds_per_page)} per page, {callsPerFax(group.attempts_per_delivered)} calls
                  per delivered fax
                </Typography>
              </CardContent>
            </Card>
          ))}
        </Stack>
      ) : (
        <Table size="small" aria-label="How fax calls went" sx={{ mt: 1 }}>
          <TableHead>
            <TableRow>
              <TableCell>Compression and error correction</TableCell>
              <TableCell>Speed</TableCell>
              <TableCell align="right">Calls</TableCell>
              <TableCell align="right">Succeeded</TableCell>
              <TableCell align="right">Seconds per page</TableCell>
              <TableCell align="right">Calls per delivered fax</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {groups.map((group) => (
              <TableRow key={`${group.coding_label}|${group.speed_label}`}>
                <TableCell>{group.coding_label}</TableCell>
                <TableCell>{group.speed_label}</TableCell>
                <TableCell align="right">{group.calls}</TableCell>
                <TableCell align="right">{group.success_percent}%</TableCell>
                <TableCell align="right">{secondsPerPage(group.seconds_per_page)}</TableCell>
                <TableCell align="right">{callsPerFax(group.attempts_per_delivered)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      ))}
      {groups.length > 0 && (
        <Typography variant="caption" color="text.secondary" display="block" sx={{ mt: 1 }}>
          Seconds per page: the time of every call in the row, divided by the pages they confirmed. Calls per
          delivered fax: sent faxes only.
        </Typography>
      )}
      {summary && <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>{summary.note}</Typography>}
    </Box>
  );
}
