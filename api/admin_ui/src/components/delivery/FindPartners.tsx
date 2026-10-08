// Partners → Find partners: recipients that run Faxbot, found from your fax calls, partners' introductions and
// directories you trust; introducing two partners who agreed; publishing your own number. A suggestion is only a
// hint: enrolling makes a partner whose number is still confirmed with a code by fax.
import { useCallback, useEffect, useState } from 'react';
import {
  Alert, Box, Button, Chip, CircularProgress, FormControlLabel, MenuItem, Paper, Stack, Switch, Table, TableBody,
  TableCell, TableContainer, TableHead, TableRow, TextField, Typography,
} from '@mui/material';
import AdminAPIClient from '../../api/client';
import type { Discovery, DiscoverySuggestion } from '../../api/discoveryTypes';
import { formatServerTime } from '../../api/time';
import type { AdminDestination } from '../../navigation';
import { DeliveryError, Notice } from './shared';

export const NO_SUGGESTIONS = 'No recipient that runs Faxbot has been found yet.';
export const INTRODUCE_TEXT = "Introduce two partners who both agreed. Each gets the other's details and confirms "
  + 'them with a code by fax.';
const SOURCE_LABEL: Record<DiscoverySuggestion['source'], string> = {
  call: 'From a fax call', introduction: 'Introduced', directory: 'From a directory',
};

function Part({ title, text, children, testId }: { title: string; text?: string | null; children: React.ReactNode; testId: string }) {
  return (
    <Paper variant="outlined" sx={{ p: 2, borderRadius: 2 }} data-testid={testId}>
      <Typography variant="subtitle1" component="h3">{title}</Typography>
      {text && <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>{text}</Typography>}
      {children}
    </Paper>
  );
}

function SuggestionCard({ item, canWrite, busy, onEnroll, onDismiss }: {
  item: DiscoverySuggestion; canWrite: boolean; busy: boolean; onEnroll: () => void; onDismiss: () => void;
}) {
  return (
    <Box data-testid="discovery-suggestion">
      <Box display="flex" gap={1} alignItems="center" flexWrap="wrap">
        <Typography variant="subtitle1">{item.organization}</Typography>
        <Chip size="small" variant="outlined" label={SOURCE_LABEL[item.source]} />
      </Box>
      <Typography variant="body2" color="text.secondary">{item.number}</Typography>
      <Typography variant="body2" sx={{ mt: 0.5 }}>{item.sentence}</Typography>
      <Typography variant="body2" color="text.secondary">{item.source_text}</Typography>
      {item.network_text && (
        <Alert severity="info" sx={{ mt: 1, borderRadius: 2 }}>
          {item.network_text}
          {item.certificate && (
            <Typography variant="body2" component="div" sx={{ mt: 0.5, fontFamily: 'monospace', wordBreak: 'break-all' }}>
              {`Certificate fingerprint: ${item.certificate}`}
            </Typography>
          )}
        </Alert>
      )}
      {canWrite && (
        <Box display="flex" gap={1} mt={1}>
          <Button size="small" variant="outlined" sx={{ borderRadius: 2 }} disabled={busy} onClick={onEnroll}>Enroll as partner</Button>
          <Button size="small" disabled={busy} onClick={onDismiss}>Dismiss</Button>
        </Box>
      )}
    </Box>
  );
}

export default function FindPartners({ client, canWrite, onChanged }: {
  client: AdminAPIClient; canWrite: boolean; onChanged?: () => void;
}) {
  const [data, setData] = useState<Discovery | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [directories, setDirectories] = useState('');
  const [lookup, setLookup] = useState('');
  const [first, setFirst] = useState('');
  const [second, setSecond] = useState('');
  const [directory, setDirectory] = useState('');

  const load = useCallback(async () => {
    try {
      const loaded = await client.getDiscovery();
      setData(loaded);
      setDirectories(loaded.settings.directories.join('\n'));
    } catch (failure) {
      setError(failure);
    }
  }, [client]);

  useEffect(() => { void load(); }, [load]);

  const run = async (operation: () => Promise<string | null>, changesPartners = false) => {
    setBusy(true);
    setError(null);
    try {
      const message = await operation();
      if (message) setNotice(message);
      await load();
      if (changesPartners) onChanged?.();
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };

  if (!data) {
    return error ? <DeliveryError error={error} onClose={() => setError(null)} /> : <CircularProgress size={24} />;
  }
  const verified = data.partners.filter((partner) => partner.verified);
  const saveSettings = (change: Parameters<AdminAPIClient['saveDiscoverySettings']>[0]) => void run(
    async () => (await client.saveDiscoverySettings(change)).detail);
  const saveDirectories = () => saveSettings({
    directories: directories.split(/[\s,]+/).map((value) => value.trim()).filter(Boolean),
  });

  return (
    <Stack spacing={2} data-testid="find-partners">
      <Notice message={notice} onClose={() => setNotice(null)} />
      <DeliveryError error={error} onClose={() => setError(null)} />
      {!data.direct_delivery && <Alert severity="info" sx={{ borderRadius: 2 }}>{data.texts.well_known}</Alert>}

      <Part title="Suggested partners" testId="discovery-suggestions">
        {data.suggestions.length === 0 ? (
          <Typography variant="body2" color="text.secondary">{NO_SUGGESTIONS}</Typography>
        ) : (
          <Stack spacing={2}>
            {data.suggestions.map((item) => (
              <SuggestionCard key={item.id} item={item} canWrite={canWrite} busy={busy}
                onEnroll={() => void run(async () => (await client.enrollSuggestion(item.id)).detail, true)}
                onDismiss={() => void run(async () => (await client.dismissSuggestion(item.id)).detail)} />
            ))}
          </Stack>
        )}
      </Part>

      <Part title="How Faxbot finds partners" testId="discovery-settings">
        <Stack spacing={1.5}>
          <Box>
            <FormControlLabel label="Answer Faxbot lookups" control={
              <Switch checked={data.settings.well_known} disabled={!canWrite || busy}
                onChange={(event) => saveSettings({ well_known: event.target.checked })} />} />
            <Typography variant="body2" color="text.secondary">{data.texts.well_known}</Typography>
          </Box>
          <Box>
            <FormControlLabel label="Look for Faxbot on calls" control={
              <Switch checked={data.settings.from_calls} disabled={!canWrite || busy}
                onChange={(event) => saveSettings({ from_calls: event.target.checked })} />} />
            <Typography variant="body2" color="text.secondary">{data.texts.from_calls}</Typography>
            {data.texts.private && <Typography variant="body2" color="text.secondary">{data.texts.private}</Typography>}
          </Box>
          <Box>
            <Typography variant="body2" sx={{ fontWeight: 600 }}>Trusted directories</Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>{data.texts.directories}</Typography>
            {canWrite && (
              <Box display="flex" gap={1} flexWrap="wrap" alignItems="flex-start">
                <TextField size="small" multiline minRows={2} label="Directories, one per line" value={directories}
                  onChange={(event) => setDirectories(event.target.value)} sx={{ minWidth: 280 }} />
                <Button variant="outlined" sx={{ borderRadius: 2 }} disabled={busy} onClick={saveDirectories}>Save directories</Button>
              </Box>
            )}
          </Box>
          {canWrite && data.settings.directories.length > 0 && (
            <Box display="flex" gap={1} flexWrap="wrap" alignItems="center">
              <TextField size="small" label="Fax number to look up" value={lookup} onChange={(event) => setLookup(event.target.value)} />
              <Button variant="outlined" sx={{ borderRadius: 2 }} disabled={busy || !lookup.trim()}
                onClick={() => void run(async () => (await client.lookUpPartner(lookup.trim())).detail)}>
                Look up
              </Button>
            </Box>
          )}
        </Stack>
      </Part>

      <Part title="Introductions" text={INTRODUCE_TEXT} testId="discovery-introductions">
        {verified.length === 0 ? (
          <Typography variant="body2" color="text.secondary">Only verified partners can be introduced, and you have none yet.</Typography>
        ) : (
          <Stack spacing={1}>
            {verified.map((partner) => (
              <Box key={partner.id}>
                <FormControlLabel label={`${partner.organization} may be introduced`} control={
                  <Switch checked={partner.may_introduce} disabled={!canWrite || busy}
                    onChange={(event) => void run(async () => (
                      await client.setMayIntroduce(partner.id, event.target.checked)).detail)} />} />
                {partner.certificate_text && (
                  <Alert severity="warning" sx={{ borderRadius: 2 }}>{`${partner.organization}: ${partner.certificate_text}`}</Alert>
                )}
              </Box>
            ))}
            {canWrite && verified.length > 1 && (
              <Box display="flex" gap={1} flexWrap="wrap" alignItems="center">
                <TextField select size="small" label="Introduce" value={first} onChange={(event) => setFirst(event.target.value)}
                  sx={{ minWidth: 200 }}>
                  {verified.map((partner) => <MenuItem key={partner.id} value={partner.id}>{partner.organization}</MenuItem>)}
                </TextField>
                <TextField select size="small" label="To" value={second} onChange={(event) => setSecond(event.target.value)}
                  sx={{ minWidth: 200 }}>
                  {verified.filter((partner) => partner.id !== first).map((partner) => (
                    <MenuItem key={partner.id} value={partner.id}>{partner.organization}</MenuItem>))}
                </TextField>
                <Button variant="outlined" sx={{ borderRadius: 2 }} disabled={busy || !first || !second}
                  onClick={() => void run(async () => (await client.introducePartners(first, second)).detail)}>
                  Introduce
                </Button>
              </Box>
            )}
            {data.introductions.map((item) => (
              <Typography key={item.id} variant="body2" color="text.secondary">
                {`${formatServerTime(item.when)}: ${item.sentence}`}
              </Typography>
            ))}
          </Stack>
        )}
      </Part>

      <Part title="Publish your number" text={data.publishable.sentence} testId="discovery-publish">
        <Stack spacing={1.5}>
          {canWrite && data.publishable.receives && data.publishable.number && (
            <Box display="flex" gap={1} flexWrap="wrap" alignItems="center">
              <TextField size="small" label="Directory you control" placeholder="faxdirectory.example.org" value={directory}
                onChange={(event) => setDirectory(event.target.value)} />
              <Button variant="outlined" sx={{ borderRadius: 2 }} disabled={busy || !directory.trim()}
                onClick={() => void run(async () => (
                  await client.publishNumber(data.publishable.number as string, directory.trim())).detail)}>
                Publish {data.publishable.number}
              </Button>
            </Box>
          )}
          {data.publications.map((item) => (
            <Box key={item.id} data-testid="discovery-publication">
              <Typography variant="body2" sx={{ fontWeight: 600 }}>{`${item.number} in ${item.directory}`}</Typography>
              <Typography variant="body2">{item.sentence}</Typography>
              <Typography variant="body2" color="text.secondary">{`Valid until ${item.expires_text}.`}</Typography>
              <Typography variant="body2" component="pre" sx={{ fontFamily: 'monospace', whiteSpace: 'pre-wrap', wordBreak: 'break-all', my: 1 }}>
                {item.zone}
              </Typography>
              {canWrite && (
                <Box display="flex" gap={1}>
                  <Button size="small" disabled={busy}
                    onClick={() => void navigator.clipboard?.writeText(item.zone).then(() => setNotice('Record copied.'))}>
                    Copy record
                  </Button>
                  <Button size="small" disabled={busy}
                    onClick={() => void run(async () => (await client.checkPublication(item.id)).detail)}>Check</Button>
                  <Button size="small" color="error" disabled={busy}
                    onClick={() => void run(async () => (await client.withdrawPublication(item.id)).detail)}>Withdraw</Button>
                </Box>
              )}
            </Box>
          ))}
        </Stack>
      </Part>

      {data.lookups.length > 0 && (
        <Part title="Latest lookups" testId="discovery-lookups">
          <TableContainer>
            <Table size="small">
              <TableHead>
                <TableRow><TableCell>When</TableCell><TableCell>Address</TableCell><TableCell>Result</TableCell></TableRow>
              </TableHead>
              <TableBody>
                {data.lookups.map((item, index) => (
                  <TableRow key={`${item.when}-${index}`}>
                    <TableCell>{formatServerTime(item.when)}</TableCell>
                    <TableCell>{item.host}</TableCell>
                    <TableCell>{item.sentence}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableContainer>
        </Part>
      )}
    </Stack>
  );
}

// Costs → Recommendations: recipients that run Faxbot, each a partner away from no call at all.
export function DiscoveryRecommendations({ client, onCount, onNavigate }: {
  client: AdminAPIClient; onCount?: (count: number | null) => void; onNavigate?: (destination: AdminDestination) => void;
}) {
  const [data, setData] = useState<Discovery | null>(null);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    let live = true;
    client.getDiscovery().then((loaded) => {
      if (!live) return;
      setData(loaded);
      onCount?.(loaded.suggestions.length);
    }).catch((failure) => {
      if (!live) return;
      setError(failure);
      onCount?.(null);
    });
    return () => { live = false; };
  }, [client, onCount]);
  const open = () => {
    if (onNavigate) onNavigate('recipients/partners' as AdminDestination);
    else window.location.hash = '#/recipients/partners';
  };
  return (
    <Box data-testid="advice-discovery">
      <Typography variant="h5" component="h2" sx={{ mb: 1 }}>Recipients that run Faxbot</Typography>
      <DeliveryError error={error} onClose={() => setError(null)} />
      {!data && !error && <CircularProgress size={24} />}
      {data && (
        <Paper variant="outlined" sx={{ p: 2, borderRadius: 2 }}>
          {data.suggestions.length === 0 ? (
            <Typography variant="body1">{NO_SUGGESTIONS}</Typography>
          ) : (
            <Stack spacing={1.5}>
              {data.suggestions.map((item) => (
                <Box key={item.id} data-testid="advice-discovery-item">
                  <Typography variant="subtitle1">{item.organization}</Typography>
                  <Typography variant="body2" color="text.secondary">{item.number}</Typography>
                  <Typography variant="body2" sx={{ mt: 0.5 }}>{item.sentence}</Typography>
                </Box>
              ))}
              <Box>
                <Button size="small" variant="outlined" sx={{ borderRadius: 2 }} onClick={open}>Open Partners</Button>
              </Box>
            </Stack>
          )}
        </Paper>
      )}
    </Box>
  );
}
