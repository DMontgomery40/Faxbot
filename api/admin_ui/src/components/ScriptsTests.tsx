import React, { useEffect, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  IconButton,
  Link,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Tooltip,
  Typography,
} from '@mui/material';
import {
  ContentCopy as CopyIcon,
  MoveToInbox as InboundIcon,
  Router as EngineIcon,
  Terminal as TerminalIcon,
  Link as LinkIcon,
} from '@mui/icons-material';
import AdminAPIClient from '../api/client';
import type { EngineView } from '../api/client';
import { docsLink } from '../docsLinks';
import type { AdminDestination } from '../navigation';
import { providerLabel } from '../providerLabels';
import { ResponsiveFormSection, ResponsiveTextField } from './common/ResponsiveFormFields';

interface Props {
  client: AdminAPIClient;
  onNavigate: (destination: AdminDestination) => void;
  docsBase?: string;
}

type Callbacks = {
  backend?: string;
  callbacks?: Array<{ name: string; url: string; notes?: string }>;
  receiving?: { ready: boolean; message: string };
};

type EngineResult = { title: string; columns: string[]; rows: string[][]; available: boolean; message: string | null };

// Providers with their own setup guide.
const GUIDES = ['phaxio', 'sinch', 'signalwire', 'documo', 'humblefax', 'efax', 'sip', 'freeswitch'] as const;

const ENGINE_VIEWS: Array<{ id: EngineView; label: string }> = [
  { id: 'registrations', label: 'Trunk sign-ins' },
  { id: 'contacts', label: 'Checked addresses' },
  { id: 'calls', label: 'Calls in progress' },
  { id: 'faxes', label: 'Faxes in progress' },
];

function Output({ text }: { text: string }) {
  return (
    <Box component="pre" sx={{ m: 0, p: 1.5, bgcolor: 'action.hover', borderRadius: 1, fontFamily: 'monospace',
      fontSize: '0.8rem', whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', maxHeight: 320, overflow: 'auto' }}>
      {text}
    </Box>
  );
}

const ScriptsTests: React.FC<Props> = ({ client, onNavigate, docsBase }) => {
  const [inboundEnabled, setInboundEnabled] = useState(false);
  const [callbacks, setCallbacks] = useState<Callbacks | null>(null);
  const [toNumber, setToNumber] = useState('');
  const [adding, setAdding] = useState(false);
  const [added, setAdded] = useState(false);
  const [addError, setAddError] = useState('');
  const [engine, setEngine] = useState<EngineResult | null>(null);
  const [engineView, setEngineView] = useState<EngineView | null>(null);
  const [engineError, setEngineError] = useState('');
  const [actions, setActions] = useState<Array<{ id: string; label: string }>>([]);
  const [actionId, setActionId] = useState('');
  const [actionText, setActionText] = useState('');
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState('');

  useEffect(() => {
    void (async () => {
      try {
        const settings = await client.getSettings();
        setInboundEnabled(Boolean((settings as any)?.inbound?.enabled));
      } catch { /* the cards below say what they need */ }
      try { setCallbacks(await client.getInboundCallbacks()); } catch { setCallbacks(null); }
      try {
        const listed = await client.listActions();
        if (listed?.enabled && Array.isArray(listed.items)) setActions(listed.items.map((item) => ({ id: item.id, label: item.label })));
      } catch { setActions([]); }
    })();
  }, [client]);

  const receiver = callbacks?.backend ? providerLabel(callbacks.backend) : '';
  const guide = GUIDES.find((page) => page === callbacks?.backend);

  const addTestFax = async () => {
    setAdding(true); setAdded(false); setAddError('');
    try {
      await client.simulateInbound({ ...(toNumber.trim() ? { to: toNumber.trim() } : {}), pages: 1, status: 'received' });
      setAdded(true);
    } catch {
      setAddError("The test fax couldn't be added. Check that receiving is on, then try again.");
    } finally {
      setAdding(false);
    }
  };

  const showEngine = async (view: EngineView) => {
    setEngineView(view); setBusy(true); setEngineError('');
    try {
      setEngine(await client.getEngineView(view));
    } catch {
      setEngine(null);
      setEngineError('Faxbot could not ask its fax engine. Try again in a moment.');
    } finally {
      setBusy(false);
    }
  };

  const runAction = async (id: string) => {
    setActionId(id); setBusy(true); setActionText('');
    try {
      const result = await client.runAction(id);
      setActionText((result.stdout || '') + (result.stderr ? `\n${result.stderr}` : '') || 'No output.');
    } catch (err) {
      setActionText(err instanceof Error ? err.message : 'The check did not run.');
    } finally {
      setBusy(false);
    }
  };

  const copy = async (url: string) => {
    try {
      await navigator.clipboard.writeText(url);
      setCopied(url);
    } catch { setCopied(''); }
  };

  return (
    <Box>
      <Typography variant="h4" component="h1" gutterBottom>Scripts & checks</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
        Tools for checking Faxbot by hand. Nothing here sends a fax or changes a setting.{' '}
        <Link href={docsLink('scripts', docsBase)} target="_blank" rel="noreferrer">How to use them</Link>
      </Typography>

      <Stack spacing={3}>
        <ResponsiveFormSection title="Add a test fax" subtitle="A one-page fax in Received, marked as a test everywhere" icon={<InboundIcon />}>
          {inboundEnabled ? (
            <Stack spacing={2}>
              <Typography variant="body2" color="text.secondary">
                The test fax goes through the same steps as a real one{receiver ? ` from ${receiver}` : ''}: owners, mailbox
                rules and email delivery. No call is made.
              </Typography>
              <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2} alignItems={{ sm: 'center' }}>
                <ResponsiveTextField label="Your fax number it arrives on (optional)" value={toNumber} onChange={setToNumber} />
                <Button variant="contained" onClick={addTestFax} disabled={adding}
                  startIcon={adding ? <CircularProgress size={16} color="inherit" /> : <InboundIcon />}>
                  Add a test fax
                </Button>
              </Stack>
              {added && (
                <Alert severity="success" onClose={() => setAdded(false)}
                  action={<Button color="inherit" size="small" onClick={() => onNavigate('faxes/received')}>Open Received</Button>}>
                  A test fax was added to Received.
                </Alert>
              )}
              {addError && <Alert severity="error" onClose={() => setAddError('')}>{addError}</Alert>}
            </Stack>
          ) : (
            <Typography variant="body2" color="text.secondary">Receiving is turned off, so there is nowhere to add a test fax.</Typography>
          )}
        </ResponsiveFormSection>

        <ResponsiveFormSection title={receiver ? `How ${receiver} reaches Faxbot` : 'How received faxes reach Faxbot'}
          subtitle="What the provider that receives your faxes needs from you" icon={<LinkIcon />}>
          {!callbacks ? (
            <Typography variant="body2" color="text.secondary">Faxbot could not read its receiving settings.</Typography>
          ) : callbacks.receiving ? (
            <Alert severity={callbacks.receiving.ready ? 'success' : 'warning'}>{callbacks.receiving.message}</Alert>
          ) : callbacks.callbacks && callbacks.callbacks.length > 0 ? (
            <Stack spacing={2}>
              {callbacks.callbacks.map((item) => (
                <Box key={item.url}>
                  <Typography variant="subtitle2">{item.name}</Typography>
                  <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                    <Typography variant="body2" sx={{ fontFamily: 'monospace', overflowWrap: 'anywhere' }}>{item.url}</Typography>
                    <Tooltip title={copied === item.url ? 'Copied' : 'Copy address'}>
                      <IconButton size="small" aria-label={`Copy ${item.name} address`} onClick={() => void copy(item.url)}><CopyIcon fontSize="small" /></IconButton>
                    </Tooltip>
                  </Box>
                  {item.notes && <Typography variant="body2" color="text.secondary">{item.notes}</Typography>}
                </Box>
              ))}
              {guide && <Link href={docsLink(guide, docsBase)} target="_blank" rel="noreferrer">{receiver} setup guide</Link>}
            </Stack>
          ) : (
            <Typography variant="body2" color="text.secondary">
              {receiver ? `Faxbot collects received faxes from ${receiver} itself, so there is no address to give ${receiver}.`
                : 'No provider receives faxes for Faxbot.'}
            </Typography>
          )}
        </ResponsiveFormSection>

        <ResponsiveFormSection title="Fax engine" subtitle="What Faxbot's own fax engine reports right now" icon={<EngineIcon />}>
          <Stack spacing={2}>
            <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap' }}>
              {ENGINE_VIEWS.map((view) => (
                <Button key={view.id} size="small" variant={engineView === view.id ? 'contained' : 'outlined'}
                  disabled={busy} onClick={() => void showEngine(view.id)}>{view.label}</Button>
              ))}
            </Box>
            {engineError && <Alert severity="error">{engineError}</Alert>}
            {engine && engine.rows.length > 0 && (
              <Box sx={{ overflowX: 'auto' }}>
                <Table size="small" aria-label={engine.title}>
                  <TableHead><TableRow>{engine.columns.map((column) => <TableCell key={column}>{column}</TableCell>)}</TableRow></TableHead>
                  <TableBody>
                    {engine.rows.map((row, index) => (
                      <TableRow key={index}>{row.map((cell, cellIndex) => <TableCell key={cellIndex} sx={{ overflowWrap: 'anywhere' }}>{cell}</TableCell>)}</TableRow>
                    ))}
                  </TableBody>
                </Table>
              </Box>
            )}
            {engine?.message && <Typography variant="body2" color="text.secondary">{engine.message}</Typography>}
          </Stack>
        </ResponsiveFormSection>

        {actions.length > 0 && (
          <ResponsiveFormSection title="Server checks" subtitle="Commands Faxbot runs on its own server, read-only" icon={<TerminalIcon />}>
            <Stack spacing={2}>
              <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap' }}>
                {actions.map((action) => (
                  <Button key={action.id} size="small" variant={actionId === action.id ? 'contained' : 'outlined'}
                    disabled={busy} onClick={() => void runAction(action.id)}>{action.label}</Button>
                ))}
              </Box>
              {actionId && (busy && !actionText ? <CircularProgress size={20} /> : actionText && <Output text={actionText} />)}
            </Stack>
          </ResponsiveFormSection>
        )}
      </Stack>
    </Box>
  );
};

export default ScriptsTests;
