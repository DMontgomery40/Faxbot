// Providers → the trunk page: copiers that fax over the network (copiers.py) and copiers as Faxbot's own extensions
// (copier_extensions.py). Which makers document SIP fax with T.38, what to set on each, where it was read, and your
// copier extensions: add one (its password is shown once), see where its faxes go, take it away.
import { useCallback, useEffect, useState } from 'react';
import {
  Accordion, AccordionDetails, AccordionSummary, Alert, Box, Button, Link, MenuItem, Stack, TextField, Typography,
} from '@mui/material';
import ExpandMoreIcon from '@mui/icons-material/ExpandMore';
import { AdminAPIError } from '../api/client';

// A source's read date in the reader's own words ('2026-10-10' is a calendar day, not a moment).
function readOn(day: string): string {
  const [year, month, date] = day.split('-').map(Number);
  if (!year || !month || !date) return day;
  return new Date(year, month - 1, date).toLocaleDateString(undefined, { day: 'numeric', month: 'long', year: 'numeric' });
}

type Call = <T>(request: { method: string; path: string; body?: unknown }) => Promise<T>;

interface CopierItem {
  id: string;
  label: string;
  sip_t38: boolean;
  summary: string;
  steps: string[];
  sources: Array<{ url: string; read_on: string }>;
}

export interface CopierExtension {
  id: string;
  name: string;
  extension: string;
  username: string;
  mailbox_id: string | null;
  mailbox: string | null;
  numbers: string[];
  receive: 'copier' | 'intake';
  sentence: string;
}

interface Created extends CopierExtension {
  password: string;
  realm: string;
  password_text: string;
  apply_text: string;
}

function problem(error: unknown, fallback: string): string {
  if (error instanceof AdminAPIError && error.status === 403) return 'You do not have permission to do this.';
  if (error instanceof AdminAPIError && error.detail) return error.detail;
  return fallback;
}

function AddExtension({ call, mailboxes, onAdded }: {
  call: Call; mailboxes: Array<{ id: string; label: string }>; onAdded: (created: Created) => void;
}) {
  const [name, setName] = useState('');
  const [extension, setExtension] = useState('');
  const [mailbox, setMailbox] = useState('');
  const [numbers, setNumbers] = useState('');
  const [receive, setReceive] = useState<'copier' | 'intake'>('copier');
  const [message, setMessage] = useState<string | null>(null);
  const add = async () => {
    try {
      const created = await call<Created>({ method: 'POST', path: '/admin/sip/copiers/extensions', body: {
        name: name.trim(), extension: extension.trim(), mailbox_id: mailbox || null, receive,
        numbers: numbers.split(',').map((item) => item.trim()).filter(Boolean) } });
      setMessage(null);
      setName(''); setExtension(''); setNumbers('');
      onAdded(created);
    } catch (failure) {
      setMessage(problem(failure, 'The copier could not be added. Try again.'));
    }
  };
  return (
    <Box sx={{ mt: 1 }}>
      <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} flexWrap="wrap">
        <TextField size="small" label="Copier name" value={name} onChange={(event) => setName(event.target.value)} />
        <TextField size="small" label="Extension" placeholder="601" value={extension}
          onChange={(event) => setExtension(event.target.value)} inputProps={{ 'data-testid': 'copier-extension' }} />
        <TextField select size="small" label="Sends from mailbox" value={mailbox} sx={{ minWidth: 180 }}
          onChange={(event) => setMailbox(event.target.value)}>
          <MenuItem value="">No mailbox</MenuItem>
          {mailboxes.map((item) => <MenuItem key={item.id} value={item.id}>{item.label}</MenuItem>)}
        </TextField>
        <TextField size="small" label="Its fax numbers" placeholder="+13035550161" value={numbers}
          onChange={(event) => setNumbers(event.target.value)} />
        <TextField select size="small" label="Faxes to its numbers" value={receive} sx={{ minWidth: 200 }}
          onChange={(event) => setReceive(event.target.value as 'copier' | 'intake')}>
          <MenuItem value="copier">Go on to the copier</MenuItem>
          <MenuItem value="intake">Stay in Received</MenuItem>
        </TextField>
        <Button variant="outlined" onClick={() => void add()} disabled={!name.trim() || !extension.trim()}>
          Add copier
        </Button>
      </Stack>
      {message && <Alert severity="warning" sx={{ mt: 1 }}>{message}</Alert>}
    </Box>
  );
}

export default function CopiersPanel({ call }: { call: Call }) {
  const [items, setItems] = useState<CopierItem[] | null>(null);
  const [extensions, setExtensions] = useState<CopierExtension[]>([]);
  const [mailboxes, setMailboxes] = useState<Array<{ id: string; label: string }>>([]);
  const [created, setCreated] = useState<Created | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const load = useCallback(() => {
    let live = true;
    call<{ copiers: CopierItem[]; extensions?: CopierExtension[] }>({ method: 'GET', path: '/admin/sip/copiers' })
      .then((found) => { if (live) { setItems(found.copiers ?? []); setExtensions(found.extensions ?? []); } })
      .catch(() => { if (live) setItems(null); });
    call<{ items?: Array<{ id: string; label: string }> }>({ method: 'GET', path: '/access/mailboxes?limit=200' })
      .then((found) => { if (live) setMailboxes(found.items ?? []); })
      .catch(() => { if (live) setMailboxes([]); });
    return () => { live = false; };
  }, [call]);
  useEffect(() => load(), [load]);
  const remove = async (item: CopierExtension) => {
    try {
      const done = await call<{ detail: string }>({ method: 'POST', path: `/admin/sip/copiers/extensions/${item.id}/remove` });
      setMessage(done.detail);
      setCreated(null);
      load();
    } catch (failure) {
      setMessage(problem(failure, 'The copier could not be taken away. Try again.'));
    }
  };
  if (!items) return null;
  return (
    <Accordion disableGutters variant="outlined" data-testid="copiers-panel">
      <AccordionSummary expandIcon={<ExpandMoreIcon />}>
        <Typography>Copiers that fax through Faxbot</Typography>
      </AccordionSummary>
      <AccordionDetails>
        <Typography variant="body2" sx={{ mb: 1 }}>
          A copier with an IP fax option can register to Faxbot as its own extension and keep its Fax button: Faxbot
          takes its faxes and sends them on through your routes, rules and holds, and sends the faxes for its numbers
          on to it. A copier never reaches your trunk directly.
        </Typography>
        {extensions.map((item) => (
          <Stack key={item.id} direction="row" spacing={1} alignItems="center">
            <Typography variant="body2">{item.sentence}</Typography>
            <Button size="small" color="error" onClick={() => void remove(item)}>Take it away</Button>
          </Stack>
        ))}
        {created && (
          <Alert severity="success" sx={{ my: 1 }} data-testid="copier-created">
            <Typography variant="body2">{created.password_text}</Typography>
            <Typography variant="body2">{`User name: ${created.username}`}</Typography>
            <Typography variant="body2">{`Password: ${created.password}`}</Typography>
            <Typography variant="body2">{`Realm: ${created.realm}`}</Typography>
            <Typography variant="body2">{created.apply_text}</Typography>
          </Alert>
        )}
        {message && <Alert severity="info" sx={{ my: 1 }}>{message}</Alert>}
        <AddExtension call={call} mailboxes={mailboxes} onAdded={(found) => { setCreated(found); load(); }} />
        <Typography variant="subtitle2" sx={{ mt: 2 }}>What each maker documents</Typography>
        {items.map((item) => (
          <Box key={item.id} sx={{ mb: 1.5 }}>
            <Typography variant="body2" fontWeight={600}>{item.label}{item.sip_t38 ? '' : ' (no SIP fax found)'}</Typography>
            <Typography variant="body2">{item.summary}</Typography>
            {item.steps.length > 0 && (
              <Box component="ol" sx={{ pl: 3, my: 0.5 }}>
                {item.steps.map((step) => <li key={step}><Typography variant="body2">{step}</Typography></li>)}
              </Box>
            )}
            {item.sources.map((source) => (
              <Typography key={source.url} variant="body2">
                <Link href={source.url} target="_blank" rel="noreferrer">{new URL(source.url).hostname}</Link>
                {`, read ${readOn(source.read_on)}`}
              </Typography>
            ))}
          </Box>
        ))}
      </AccordionDetails>
    </Accordion>
  );
}

// Received details: whether this fax was sent on to a copier, or why it stays in Received.
export function ReceivedCopier({ call, inboundId }: { call: Call; inboundId: string }) {
  const [sentence, setSentence] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    call<{ sentence: string | null }>({ method: 'GET', path: `/admin/sip/copiers/received/${encodeURIComponent(inboundId)}` })
      .then((found) => { if (live) setSentence(found.sentence); })
      .catch(() => { if (live) setSentence(null); });
    return () => { live = false; };
  }, [call, inboundId]);
  if (!sentence) return null;
  return <Alert severity="info" sx={{ mt: 2 }} data-testid="received-copier">{sentence}</Alert>;
}
