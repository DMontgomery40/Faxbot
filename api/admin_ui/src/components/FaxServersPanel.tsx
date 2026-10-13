// Providers → the trunk page: fax servers that send through Faxbot (fax_servers.py), such as OpenText RightFax with a
// Brooktrout SR140 whose Primary Gateway is Faxbot. Enrol one (its password is shown once), change where it may send,
// its limits and who is told about failures, see each fax it handed over and what became of it, take it away.
import { useCallback, useEffect, useState } from 'react';
import {
  Accordion, AccordionDetails, AccordionSummary, Alert, Box, Button, Stack, Table, TableBody, TableCell, TableHead,
  TableRow, TextField, Typography,
} from '@mui/material';
import ExpandMoreIcon from '@mui/icons-material/ExpandMore';
import { AdminAPIError } from '../api/client';
import { formatServerTime } from '../api/time';

type Call = <T>(request: { method: string; path: string; body?: unknown }) => Promise<T>;

export interface FaxServer {
  id: string;
  name: string;
  address: string;
  username: string;
  countries: string[];
  per_hour: number;
  per_day: number;
  dial_prefix: string | null;
  contact_email: string | null;
  contact_webhook: string | null;
  sentence: string;
}

interface Created extends FaxServer {
  password: string;
  realm: string;
  password_text: string;
  apply_text: string;
  where_text?: string;
}

export interface FaxServerCall {
  id: string;
  server: string | null;
  to: string | null;
  dialled: string | null;
  state: string;
  state_text: string;
  sentence: string;
  received_at: string;
  notice: string | null;
  notice_text: string | null;
}

const NOTICE_WORDS: Record<string, string> = {
  sent: 'Told', failed: 'Not told yet', uncertain: 'May have been told',
};

function problem(error: unknown, fallback: string): string {
  if (error instanceof AdminAPIError && error.status === 403) return 'You do not have permission to do this.';
  if (error instanceof AdminAPIError && error.detail) return error.detail;
  return fallback;
}

function countryList(text: string): string[] {
  return text.split(/[\s,]+/).map((item) => item.trim().toUpperCase()).filter(Boolean);
}

function whole(text: string): number | null {
  const value = Number(text.trim());
  return text.trim() && Number.isInteger(value) && value > 0 ? value : null;
}

function ServerForm({ call, onAdded }: { call: Call; onAdded: (created: Created) => void }) {
  const [name, setName] = useState('RightFax');
  const [address, setAddress] = useState('');
  const [countries, setCountries] = useState('');
  const [perHour, setPerHour] = useState('');
  const [perDay, setPerDay] = useState('');
  const [prefix, setPrefix] = useState('');
  const [email, setEmail] = useState('');
  const [webhook, setWebhook] = useState('');
  const [message, setMessage] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const add = async () => {
    setSaving(true);
    try {
      const created = await call<Created>({ method: 'POST', path: '/admin/sip/fax-servers', body: {
        name: name.trim(), address: address.trim(), countries: countryList(countries), per_hour: whole(perHour),
        per_day: whole(perDay), dial_prefix: prefix.trim() || null, contact_email: email.trim() || null,
        contact_webhook: webhook.trim() || null } });
      setMessage(null);
      setAddress(''); setCountries(''); setPerHour(''); setPerDay(''); setPrefix(''); setEmail(''); setWebhook('');
      onAdded(created);
    } catch (failure) {
      setMessage(problem(failure, 'The fax server could not be added. Try again.'));
    } finally {
      setSaving(false);
    }
  };
  return (
    <Box sx={{ mt: 1 }}>
      <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} flexWrap="wrap" useFlexGap>
        <TextField size="small" label="Fax server name" value={name} onChange={(event) => setName(event.target.value)} />
        <TextField size="small" label="Its address on your network" placeholder="192.168.1.40" value={address}
          onChange={(event) => setAddress(event.target.value)} />
        <TextField size="small" label="Countries it may send to" placeholder="US, CA" value={countries}
          helperText="Your own country when empty" onChange={(event) => setCountries(event.target.value)} />
        <TextField size="small" label="Faxes an hour" placeholder="60" value={perHour} sx={{ width: 130 }}
          onChange={(event) => setPerHour(event.target.value)} />
        <TextField size="small" label="Faxes a day" placeholder="500" value={perDay} sx={{ width: 130 }}
          onChange={(event) => setPerDay(event.target.value)} />
        <TextField size="small" label="Outside-line prefix" placeholder="9" value={prefix} sx={{ width: 160 }}
          onChange={(event) => setPrefix(event.target.value)} />
        <TextField size="small" label="Email when a fax is not delivered" value={email} sx={{ minWidth: 260 }}
          onChange={(event) => setEmail(event.target.value)} />
        <TextField size="small" label="Or your system's address for notices" placeholder="https://" value={webhook}
          sx={{ minWidth: 260 }} onChange={(event) => setWebhook(event.target.value)} />
        <Button variant="outlined" onClick={() => void add()} disabled={saving || !name.trim() || !address.trim()}>
          Add fax server
        </Button>
      </Stack>
      {message && <Alert severity="warning" sx={{ mt: 1 }}>{message}</Alert>}
    </Box>
  );
}

function Limits({ call, server, onSaved }: { call: Call; server: FaxServer; onSaved: (sentence: string) => void }) {
  const [perHour, setPerHour] = useState(String(server.per_hour));
  const [perDay, setPerDay] = useState(String(server.per_day));
  const [countries, setCountries] = useState(server.countries.join(', '));
  const [email, setEmail] = useState(server.contact_email ?? '');
  const [message, setMessage] = useState<string | null>(null);
  const save = async () => {
    try {
      const saved = await call<FaxServer>({ method: 'PUT', path: `/admin/sip/fax-servers/${server.id}`, body: {
        per_hour: whole(perHour), per_day: whole(perDay), countries: countryList(countries),
        ...(email.trim() ? { contact_email: email.trim() } : {}) } });
      setMessage(null);
      onSaved(saved.sentence);
    } catch (failure) {
      setMessage(problem(failure, 'The changes could not be saved. Try again.'));
    }
  };
  return (
    <Box sx={{ mt: 1 }}>
      <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} flexWrap="wrap" useFlexGap>
        <TextField size="small" label="Countries it may send to" value={countries}
          onChange={(event) => setCountries(event.target.value)} />
        <TextField size="small" label="Faxes an hour" value={perHour} sx={{ width: 130 }}
          onChange={(event) => setPerHour(event.target.value)} />
        <TextField size="small" label="Faxes a day" value={perDay} sx={{ width: 130 }}
          onChange={(event) => setPerDay(event.target.value)} />
        <TextField size="small" label="Email when a fax is not delivered" value={email} sx={{ minWidth: 260 }}
          onChange={(event) => setEmail(event.target.value)} />
        <Button size="small" variant="outlined" onClick={() => void save()}>Save</Button>
      </Stack>
      {message && <Alert severity="warning" sx={{ mt: 1 }}>{message}</Alert>}
    </Box>
  );
}

export function FaxServerCalls({ calls }: { calls: FaxServerCall[] }) {
  if (calls.length === 0) {
    return <Typography variant="body2" sx={{ mt: 1 }}>No faxes from fax servers yet.</Typography>;
  }
  return (
    <Table size="small" sx={{ mt: 1 }} data-testid="fax-server-calls">
      <TableHead>
        <TableRow>
          <TableCell>Received</TableCell>
          <TableCell>Fax server</TableCell>
          <TableCell>To</TableCell>
          <TableCell>State</TableCell>
          <TableCell>What happened</TableCell>
          <TableCell>Its contact</TableCell>
        </TableRow>
      </TableHead>
      <TableBody>
        {calls.map((item) => (
          <TableRow key={item.id}>
            <TableCell>{formatServerTime(item.received_at)}</TableCell>
            <TableCell>{item.server ?? '-'}</TableCell>
            <TableCell>{item.to ?? item.dialled ?? '-'}</TableCell>
            <TableCell>{item.state_text}</TableCell>
            <TableCell>{item.sentence}</TableCell>
            <TableCell title={item.notice_text ?? undefined}>{item.notice ? NOTICE_WORDS[item.notice] ?? '-' : '-'}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

export default function FaxServersPanel({ call }: { call: Call }) {
  const [servers, setServers] = useState<FaxServer[] | null>(null);
  const [where, setWhere] = useState('');
  const [calls, setCalls] = useState<FaxServerCall[]>([]);
  const [created, setCreated] = useState<Created | null>(null);
  const [changing, setChanging] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const load = useCallback(() => {
    let live = true;
    call<{ servers: FaxServer[]; where_text: string }>({ method: 'GET', path: '/admin/sip/fax-servers' })
      .then((found) => {
        if (!live) return;
        setServers(found.servers ?? []);
        setWhere(found.where_text ?? '');
        setFailed(null);
      })
      .catch((failure) => { if (live) { setServers([]); setFailed(problem(failure, 'Your fax servers could not be read. Reload to try again.')); } });
    call<{ calls: FaxServerCall[] }>({ method: 'GET', path: '/admin/sip/fax-servers/calls' })
      .then((found) => { if (live) setCalls(found.calls ?? []); })
      .catch(() => { if (live) setCalls([]); });
    return () => { live = false; };
  }, [call]);
  useEffect(() => load(), [load]);
  const remove = async (item: FaxServer) => {
    try {
      const done = await call<{ detail: string }>({ method: 'POST', path: `/admin/sip/fax-servers/${item.id}/remove` });
      setMessage(done.detail);
      setCreated(null);
      load();
    } catch (failure) {
      setMessage(problem(failure, 'The fax server could not be taken away. Try again.'));
    }
  };
  if (servers === null) return <Typography variant="body2">Loading fax servers…</Typography>;
  return (
    <Accordion disableGutters variant="outlined" data-testid="fax-servers-panel">
      <AccordionSummary expandIcon={<ExpandMoreIcon />}>
        <Typography>Fax servers that send through Faxbot (RightFax and others)</Typography>
      </AccordionSummary>
      <AccordionDetails>
        <Typography variant="body2" sx={{ mb: 1 }}>
          A fax server such as RightFax can send its faxes to Faxbot instead of a fax board&apos;s phone line: Faxbot
          takes each whole fax and sends it on by your routes, rules and holds, so every saving applies. Faxbot
          answers only the fax servers you add here, from their own address on your network, and refuses a number
          your dialing rules, its countries or its limits don&apos;t allow before answering, so the fax server shows
          that fax as failed.
        </Typography>
        <Typography variant="body2" sx={{ mb: 1 }}>
          The fax server shows a fax as sent once Faxbot has it. If Faxbot then cannot deliver it, the list below
          says so, and Faxbot tells the contact you set. A fax that may have arrived is never sent again.
        </Typography>
        {failed && <Alert severity="warning" sx={{ my: 1 }}>{failed}</Alert>}
        {where && <Alert severity="info" sx={{ my: 1 }}>{where}</Alert>}
        {servers.map((item) => (
          <Box key={item.id} sx={{ mb: 1 }}>
            <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap">
              <Typography variant="body2">{item.sentence}</Typography>
              <Typography variant="body2" color="text.secondary">{`User name: ${item.username}`}</Typography>
              <Button size="small" onClick={() => setChanging(changing === item.id ? null : item.id)}>Change</Button>
              <Button size="small" color="error" onClick={() => void remove(item)}>Take it away</Button>
            </Stack>
            {changing === item.id && (
              <Limits call={call} server={item} onSaved={(sentence) => { setMessage(sentence); setChanging(null); load(); }} />
            )}
          </Box>
        ))}
        {created && (
          <Alert severity="success" sx={{ my: 1 }} data-testid="fax-server-created">
            <Typography variant="body2">{created.password_text}</Typography>
            <Typography variant="body2">{`User name: ${created.username}`}</Typography>
            <Typography variant="body2">{`Password: ${created.password}`}</Typography>
            <Typography variant="body2">{`Realm: ${created.realm}`}</Typography>
            <Typography variant="body2">{created.apply_text}</Typography>
          </Alert>
        )}
        {message && <Alert severity="info" sx={{ my: 1 }}>{message}</Alert>}
        <ServerForm call={call} onAdded={(found) => { setCreated(found); load(); }} />
        <Typography variant="subtitle2" sx={{ mt: 2 }}>Faxes your fax servers handed to Faxbot</Typography>
        <FaxServerCalls calls={calls} />
      </AccordionDetails>
    </Accordion>
  );
}
