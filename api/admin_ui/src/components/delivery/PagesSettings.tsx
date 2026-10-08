import { useEffect, useState } from 'react';
import { Alert, Box, Button, FormControlLabel, MenuItem, Stack, Switch, TextField, Typography } from '@mui/material';
import AdminAPIClient from '../../api/client';
import type { RecipientPages, RoutePages } from '../../api/sipTypes';
import { parseServerTime } from '../../api/time';
import { DeliveryError } from './shared';
import friendlyWords from './faxFriendlySetting.json';

// Dense pages. Recipients, Details: how long a page this fax machine takes (learned from calls) and this
// number's own page settings. Providers, Delivery routes: long pages for each route, and the installation's
// setting for blank space at the bottom of pages.

const TRIM_LABEL = 'Blank space at the bottom of pages';
// Fax-friendly shading (pages/friendly.py): this recipient's own choice beats the setting for all faxes. The words
// are the server's own (faxFriendlySetting.json).
const SHADING_LABEL = friendlyWords.recipient_label;
const SHADING_DEFAULT: Record<string, string> = Object.fromEntries(
  Object.entries(friendlyWords.choices).map(([value, [label]]) => [value, label.toLowerCase()]));
const TRIM_HELP = 'Machines without error correction take time for every line of a page, even a blank one. '
  + 'Faxbot leaves out the blank bottom of pages it made from your documents, never of scans or pictures.';

function learnedOn(value: string | null): string {
  const day = parseServerTime(value);
  return day ? ` Learned on ${day.toLocaleDateString(undefined, { day: 'numeric', month: 'long', year: 'numeric' })}.` : '';
}

export function RecipientPagesPanel({ client, number, canWrite }: {
  client: AdminAPIClient;
  number: string;
  canWrite: boolean;
}) {
  const [view, setView] = useState<RecipientPages | null>(null);
  const [packing, setPacking] = useState<'allow' | 'never'>('allow');
  const [trim, setTrim] = useState('');
  const [shading, setShading] = useState('');
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);

  const show = (loaded: RecipientPages) => {
    setView(loaded);
    setPacking(loaded.packing);
    setTrim(loaded.trim_blank === null ? '' : loaded.trim_blank ? 'on' : 'off');
    setShading(loaded.shading ?? '');
  };

  useEffect(() => {
    let live = true;
    setView(null);
    setError(null);
    setSaved(false);
    client.getRecipientPages(number).then((loaded) => { if (live) show(loaded); })
      .catch((failure) => { if (live) setError(failure); });
    return () => { live = false; };
  }, [client, number]);

  if (!view) return error ? <DeliveryError error={error} onClose={() => setError(null)} /> : null;

  const changed = packing !== view.packing || trim !== (view.trim_blank === null ? '' : view.trim_blank ? 'on' : 'off')
    || shading !== (view.shading ?? '');

  const save = async () => {
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      show(await client.saveRecipientPages(number, {
        packing, trim_blank: trim === '' ? null : trim === 'on',
        shading: shading === '' ? null : shading as 'always' | 'never',
      }));
      setSaved(true);
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Box mt={2} data-testid="recipient-pages">
      <Typography variant="subtitle2">Pages</Typography>
      <Typography variant="body2" sx={{ mt: 0.5 }}>
        {`${view.capability_sentence}${view.learned ? learnedOn(view.learned_at) : ''}`}
      </Typography>
      {view.ecm_sentence && <Typography variant="body2">{view.ecm_sentence}</Typography>}
      <Typography variant="body2" color="text.secondary">
        Faxbot puts several pages on one long page when this machine takes long pages and it saves pages or time.
      </Typography>
      <DeliveryError error={error} onClose={() => setError(null)} />
      {saved && <Alert severity="success" sx={{ mt: 1 }} onClose={() => setSaved(false)}>Saved for the next fax.</Alert>}
      <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2} sx={{ mt: 1 }}>
        <TextField select size="small" label="Pages per sheet" value={packing} sx={{ minWidth: 260 }}
          disabled={!canWrite || busy} onChange={(event) => setPacking(event.target.value as 'allow' | 'never')}>
          <MenuItem value="allow">As the receiving machine allows</MenuItem>
          <MenuItem value="never">Never</MenuItem>
        </TextField>
        <TextField select size="small" label={TRIM_LABEL} value={trim} sx={{ minWidth: 300 }}
          disabled={!canWrite || busy} onChange={(event) => setTrim(event.target.value)}>
          <MenuItem value="">{`As set for all faxes (${view.trim_blank_default ? 'on' : 'off'})`}</MenuItem>
          <MenuItem value="on">On for machines without error correction</MenuItem>
          <MenuItem value="off">Off</MenuItem>
        </TextField>
        <TextField select size="small" label={SHADING_LABEL} value={shading} sx={{ minWidth: 300 }}
          disabled={!canWrite || busy} onChange={(event) => setShading(event.target.value)}>
          <MenuItem value="">{`As set for all faxes (${SHADING_DEFAULT[view.shading_default ?? 'where_it_saves']})`}</MenuItem>
          <MenuItem value="always">Always</MenuItem>
          <MenuItem value="never">Never</MenuItem>
        </TextField>
      </Stack>
      {canWrite && (
        <Button size="small" sx={{ mt: 1 }} onClick={save} disabled={busy || !changed}>Save for this number</Button>
      )}
    </Box>
  );
}

export function RoutePagesPanel({ client, routes, canWrite }: {
  client: AdminAPIClient;
  routes: string[];
  canWrite: boolean;
}) {
  const [views, setViews] = useState<RoutePages[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    client.getRoutePages().then((loaded) => { if (live) setViews(loaded.routes); })
      .catch((failure) => { if (live) setError(failure); });
    return () => { live = false; };
  }, [client]);

  const save = async (route: string, body: { long_pages?: boolean; trim_blank?: boolean }) => {
    setBusy(route);
    setError(null);
    try {
      const next = await client.saveRoutePages(route, body);
      setViews((current) => (current ?? []).map((item) => (item.route === route ? next : item)));
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(null);
    }
  };

  const wanted = new Set(routes.filter(Boolean));
  const shown = (views ?? []).filter((item) => wanted.has(item.route));
  if (!shown.length) return error ? <DeliveryError error={error} onClose={() => setError(null)} /> : null;
  return (
    <Box sx={{ mt: 2 }} data-testid="route-pages">
      <Typography variant="subtitle2">Long pages</Typography>
      <DeliveryError error={error} onClose={() => setError(null)} />
      {shown.map((item) => (
        <Box key={item.route} sx={{ mt: 1 }}>
          <FormControlLabel label={`${item.label}: several pages on one long page`}
            control={<Switch checked={item.long_pages} disabled={!canWrite || !item.long_pages_possible || busy === item.route}
              onChange={(event) => void save(item.route, { long_pages: event.target.checked })} />} />
          <Typography variant="caption" color="text.secondary" display="block" sx={{ ml: 6 }}>{item.sentence}</Typography>
          {item.trim_blank !== null && (
            <>
              <FormControlLabel label={`${TRIM_LABEL}: leave it out for machines without error correction`}
                control={<Switch checked={Boolean(item.trim_blank)} disabled={!canWrite || busy === item.route}
                  onChange={(event) => void save(item.route, { trim_blank: event.target.checked })} />} />
              <Typography variant="caption" color="text.secondary" display="block" sx={{ ml: 6 }}>{TRIM_HELP}</Typography>
            </>
          )}
        </Box>
      ))}
    </Box>
  );
}
