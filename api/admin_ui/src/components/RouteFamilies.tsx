// Administration → System health → Sending routes: failures that belong to a whole route rather than to the numbers it called,
// the 2-by-2 test that tells them apart, and which providers share an upstream carrier (routing/route_families.py).
import { useCallback, useEffect, useState } from 'react';
import {
  Alert, Box, Button, Card, CardContent, MenuItem, Stack, Table, TableBody, TableCell, TableHead, TableRow,
  TextField, Typography,
} from '@mui/material';
import { Route as RouteIcon } from '@mui/icons-material';
import AdminAPIClient, { AdminAPIError } from '../api/client';
import { routeFamiliesApi, type CellState, type RouteFamilies as Families, type RouteTest } from '../api/routeFamilies';

const STATE_WORDS: Record<CellState, string> = {
  not_sent: 'Not sent', pending: 'Sending', success: 'Went through', failed: 'Failed',
};
const PLAIN = /^[A-Z][^<>{}]{3,300}[.!?]$/;

function refusal(error: unknown, fallback: string): string {
  if (error instanceof AdminAPIError && error.status === 403) return 'Your role cannot change this. Sign in with a role that can change settings.';
  if (error instanceof AdminAPIError && error.detail && PLAIN.test(error.detail.trim())) return error.detail.trim();
  return fallback;
}

function TestTable({ test, onSend, sending }: { test: RouteTest; onSend: (cell: string) => void; sending: string | null }) {
  return (
    <Box sx={{ mt: 1.5 }} data-testid={`route-test-${test.id}`}>
      <Typography variant="subtitle2">
        {test.route_a_label} and {test.route_b_label} to {test.number_a} and {test.number_b}
        {test.created_text ? `, started ${test.created_text}` : ''}
      </Typography>
      <Table size="small" sx={{ my: 1 }}>
        <TableHead>
          <TableRow><TableCell>Sent by</TableCell><TableCell>To</TableCell><TableCell>Result</TableCell><TableCell /></TableRow>
        </TableHead>
        <TableBody>
          {test.cells.map((cell) => (
            <TableRow key={cell.cell}>
              <TableCell>{cell.account_label}</TableCell>
              <TableCell>{cell.number}</TableCell>
              <TableCell>{STATE_WORDS[cell.state]}</TableCell>
              <TableCell align="right">
                {cell.state === 'not_sent' && (
                  <Button size="small" variant="outlined" disabled={sending !== null}
                    onClick={() => onSend(cell.cell)}
                    aria-label={`Send the test fax by ${cell.account_label} to ${cell.number}`}>
                    {sending === `${test.id}:${cell.cell}` ? 'Sending…' : 'Send this test fax'}
                  </Button>
                )}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
      <Typography variant="body2" color="text.secondary">{test.sentence}</Typography>
    </Box>
  );
}

export default function RouteFamilies({ client }: { client: AdminAPIClient }) {
  const api = routeFamiliesApi(client);
  const [data, setData] = useState<Families | null>(null);
  const [failed, setFailed] = useState(false);
  const [notice, setNotice] = useState<{ severity: 'success' | 'error'; text: string } | null>(null);
  const [sending, setSending] = useState<string | null>(null);
  const [plan, setPlan] = useState({ route_a: '', route_b: '', number_a: '', number_b: '' });
  const [upstream, setUpstream] = useState({ provider: '', upstream: '', source_url: '', source_date: '' });

  const load = useCallback(async () => {
    try {
      setData(await routeFamiliesApi(client).list());
      setFailed(false);
    } catch {
      setFailed(true);
    }
  }, [client]);

  useEffect(() => { void load(); }, [load]);

  const close = async (id: string) => {
    setNotice(null);
    try {
      const closed = await api.close(id);
      setNotice({ severity: 'success', text: closed.sentence });
      await load();
    } catch (error) {
      setNotice({ severity: 'error', text: refusal(error, 'The problem could not be closed. Try again in a moment.') });
    }
  };

  const startTest = async () => {
    setNotice(null);
    try {
      await api.plan(plan);
      setPlan({ route_a: '', route_b: '', number_a: '', number_b: '' });
      await load();
    } catch (error) {
      setNotice({ severity: 'error', text: refusal(error, 'The test could not be planned. Try again in a moment.') });
    }
  };

  const send = async (test: RouteTest, cell: string) => {
    setNotice(null);
    setSending(`${test.id}:${cell}`);
    try {
      const result = await api.send(test.id, cell);
      setNotice({ severity: 'success', text: result.sentence });
      await load();
    } catch (error) {
      setNotice({ severity: 'error', text: refusal(error, 'The test fax could not be sent. Try again in a moment.') });
    } finally {
      setSending(null);
    }
  };

  const saveUpstream = async () => {
    setNotice(null);
    try {
      await api.upstream(upstream.provider.trim().toLowerCase(), {
        upstream: upstream.upstream.trim() || null, source_url: upstream.source_url.trim() || null,
        source_date: upstream.source_date || null,
      });
      setUpstream({ provider: '', upstream: '', source_url: '', source_date: '' });
      await load();
    } catch (error) {
      setNotice({ severity: 'error', text: refusal(error, 'The upstream could not be saved. Try again in a moment.') });
    }
  };

  const open = data?.incidents.filter((item) => item.open) ?? [];
  const ended = data?.incidents.filter((item) => !item.open) ?? [];
  const canPlan = plan.route_a && plan.route_b && plan.number_a && plan.number_b;

  return (
    <Card sx={{ mb: 3, borderRadius: 2 }} data-testid="route-families">
      <CardContent>
        <Box display="flex" alignItems="center" justifyContent="space-between" gap={1} sx={{ mb: 1 }}>
          <Box display="flex" alignItems="center" gap={1}>
            <RouteIcon color="action" />
            <Typography variant="h6" component="h2">Sending routes</Typography>
          </Box>
          <Button size="small" onClick={() => void load()}>Check again</Button>
        </Box>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
          When many numbers fail on one route at once, the route is at fault, not the numbers. Faxbot then stops
          counting those failures against the numbers.
        </Typography>
        {failed && <Alert severity="warning">Faxbot could not read its sending routes. Check again in a moment.</Alert>}
        {notice && <Alert severity={notice.severity} sx={{ mb: 1.5 }} onClose={() => setNotice(null)}>{notice.text}</Alert>}
        {data && (
          <Stack spacing={1.5}>
            {open.length === 0 && (
              <Typography variant="body2">Faxbot has found no problem shared by a whole sending route.</Typography>
            )}
            {open.map((item) => (
              <Alert key={item.id} severity="warning"
                action={<Button color="inherit" size="small" onClick={() => void close(item.id)}>Close this problem</Button>}>
                <Typography variant="body2">{item.sentence}</Typography>
                {item.advice && <Typography variant="body2" sx={{ mt: 0.5 }}>{item.advice}</Typography>}
              </Alert>
            ))}
            {ended.slice(0, 3).map((item) => (
              <Typography key={item.id} variant="body2" color="text.secondary">{item.sentence}</Typography>
            ))}

            <Box>
              <Typography variant="subtitle1" component="h3">2-by-2 test</Typography>
              <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
                Send one test fax by each of two of your accounts to each of two of your own numbers. Each test fax
                is one real call and goes only when you send it.
              </Typography>
              <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} sx={{ mb: 1 }}>
                {(['route_a', 'route_b'] as const).map((field, index) => (
                  <TextField key={field} select size="small" sx={{ minWidth: 170 }}
                    label={index === 0 ? 'First account' : 'Second account'} value={plan[field]}
                    onChange={(event) => setPlan({ ...plan, [field]: event.target.value })}>
                    {data.accounts.map((account) => <MenuItem key={account.key} value={account.key}>{account.label}</MenuItem>)}
                  </TextField>
                ))}
                {(['number_a', 'number_b'] as const).map((field, index) => (
                  <TextField key={field} select size="small" sx={{ minWidth: 170 }}
                    label={index === 0 ? 'First number of yours' : 'Second number of yours'} value={plan[field]}
                    onChange={(event) => setPlan({ ...plan, [field]: event.target.value })}>
                    {data.numbers.map((number) => <MenuItem key={number} value={number}>{number}</MenuItem>)}
                  </TextField>
                ))}
                <Button variant="outlined" disabled={!canPlan} onClick={() => void startTest()}>Plan the test</Button>
              </Stack>
              {data.numbers.length < 2 && (
                <Typography variant="body2" color="text.secondary">
                  A 2-by-2 test needs two fax numbers of your own. Add them to your accounts in Delivery setup, Providers & accounts.
                </Typography>
              )}
              {data.tests.map((test) => (
                <TestTable key={test.id} test={test} sending={sending} onSend={(cell) => void send(test, cell)} />
              ))}
            </Box>

            <Box>
              <Typography variant="subtitle1" component="h3">Shared upstream carriers</Typography>
              <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
                Two providers that use the same upstream carrier can fail together. Record only what a provider
                publishes, with the page and the day you read it; anything else stays unknown.
              </Typography>
              {data.upstreams.length === 0 && (
                <Typography variant="body2" sx={{ mb: 1 }}>No shared upstream is recorded.</Typography>
              )}
              {data.upstreams.map((row) => (
                <Typography key={row.provider} variant="body2">
                  {row.provider} uses {row.upstream} upstream{row.source_day ? `, read on ${row.source_day}` : ''}
                  {row.source_url ? ` (${row.source_url})` : ''}.
                </Typography>
              ))}
              <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} sx={{ mt: 1 }}>
                <TextField size="small" label="Provider" value={upstream.provider}
                  onChange={(event) => setUpstream({ ...upstream, provider: event.target.value })} />
                <TextField size="small" label="Upstream carrier" value={upstream.upstream}
                  onChange={(event) => setUpstream({ ...upstream, upstream: event.target.value })} />
                <TextField size="small" label="Where it is published" value={upstream.source_url}
                  onChange={(event) => setUpstream({ ...upstream, source_url: event.target.value })} />
                <TextField size="small" type="date" label="Read on" InputLabelProps={{ shrink: true }}
                  value={upstream.source_date}
                  onChange={(event) => setUpstream({ ...upstream, source_date: event.target.value })} />
                <Button variant="outlined" disabled={!upstream.provider.trim()} onClick={() => void saveUpstream()}>
                  Save
                </Button>
              </Stack>
            </Box>
          </Stack>
        )}
      </CardContent>
    </Card>
  );
}
