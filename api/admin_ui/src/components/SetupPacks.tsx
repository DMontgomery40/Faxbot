// System → Setup → Suggested Packs: describe the organization, preview what Faxbot suggests from what it
// already knows, see what's missing and each mailbox's settings, then apply the chosen suggestions in one step.
// Previewing changes nothing; the server explains every suggestion and checks the plan again when applying.
import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert, Box, Button, Checkbox, Chip, CircularProgress, Divider, MenuItem, Paper, Stack, Table, TableBody, TableCell,
  TableHead, TableRow, TextField, Typography,
} from '@mui/material';
import type AdminAPIClient from '../api/client';
import { AdminAPIError } from '../api/client';
import { formatServerTime } from '../api/time';
import type { AdminDestination } from '../navigation';
import { formatMoney } from './delivery/shared';
import {
  applicable, appliedKeys, setupPacksApi, type ApplyResult, type ItemKind, type MailboxChoice, type MissingEntry,
  type Plan, type PlanItem, type SetupContext, type SetupPacksApi,
} from './SetupPacksApi';

const KIND_LABEL: Record<ItemKind, string> = {
  rule: 'Sending rule', setting: 'Setting', step: 'You do this', in_effect: 'Already on',
};
const OWNER_LABEL: Record<MissingEntry['owner'], string> = { you: 'Your choice', faxbot: 'Not in Faxbot yet' };
const EMPTY: SetupContext = { organization_name: '', country: '', mailboxes: {} };

function countryName(code: string): string {
  try {
    return new Intl.DisplayNames(['en'], { type: 'region' }).of(code) ?? code;
  } catch {
    return code;
  }
}

function errorText(error: unknown, fallback: string): string {
  if (error instanceof AdminAPIError && error.detail) return error.detail;
  return fallback;
}

function savingText(saving: { amount: string; currency: string } | null): string | null {
  return saving ? `About ${formatMoney(saving)} a month (estimate)` : null;
}

function CountrySelect({ label, value, countries, onChange, disabled, helperText }: {
  label: string; value: string; countries: readonly string[]; onChange: (code: string) => void;
  disabled?: boolean; helperText?: string;
}) {
  const options = useMemo(() => [...new Set([...countries, ...(value ? [value] : [])])]
    .map((code) => ({ code, name: countryName(code) }))
    .sort((a, b) => a.name.localeCompare(b.name)), [countries, value]);
  return (
    <TextField select fullWidth size="small" label={label} value={value} disabled={disabled} helperText={helperText}
      SelectProps={{ displayEmpty: true }} InputLabelProps={{ shrink: true }}
      onChange={(event) => onChange(event.target.value)}>
      <MenuItem value="">Not stated</MenuItem>
      {options.map((option) => <MenuItem key={option.code} value={option.code}>{option.name}</MenuItem>)}
    </TextField>
  );
}

function ItemRow({ item, chosen, applied, canEdit, onToggle, onNavigate }: {
  item: PlanItem; chosen: boolean; applied: boolean; canEdit: boolean; onToggle: () => void;
  onNavigate?: (destination: AdminDestination) => void;
}) {
  const choosable = applicable(item) && !applied;
  const saving = savingText(item.saving);
  return (
    <Box sx={{ display: 'flex', gap: 1, py: 1.5 }} data-testid={`setup-item-${item.key}`}>
      <Box sx={{ width: 42, flexShrink: 0 }}>
        {choosable && <Checkbox checked={chosen} disabled={!canEdit} onChange={onToggle}
          inputProps={{ 'aria-label': item.title }} sx={{ p: 0.5 }} />}
      </Box>
      <Box sx={{ flex: 1, minWidth: 0 }}>
        <Stack direction="row" spacing={1} alignItems="center" sx={{ flexWrap: 'wrap', rowGap: 0.5 }}>
          <Typography sx={{ fontWeight: 600 }}>{item.title}</Typography>
          <Chip size="small" variant="outlined" label={KIND_LABEL[item.kind]} />
          {applied && <Chip size="small" color="success" label="Applied" />}
        </Stack>
        <Typography variant="body2" sx={{ mt: 0.5 }}>{item.sentence}</Typography>
        {saving && <Typography variant="body2" sx={{ mt: 0.5, fontWeight: 600 }}>{saving}</Typography>}
        {item.sources.length > 0 && (
          <Typography variant="caption" color="text.secondary" component="p" sx={{ mt: 0.5 }}>
            From {item.sources.map((source) => `${source.name}: ${source.detail}`).join('; ')}
          </Typography>
        )}
        {item.blocked && <Alert severity="warning" sx={{ mt: 1 }}>{item.blocked}</Alert>}
        {item.kind === 'step' && item.link && onNavigate && (
          <Button size="small" sx={{ mt: 0.5, px: 0 }} onClick={() => onNavigate(item.link as AdminDestination)}>
            Open the page
          </Button>
        )}
      </Box>
    </Box>
  );
}

export default function SetupPacks({ client, api: given, countries = [], canEdit = true, onNavigate }: {
  client?: AdminAPIClient;
  api?: SetupPacksApi;
  countries?: readonly string[];
  canEdit?: boolean;
  onNavigate?: (destination: AdminDestination) => void;
}) {
  const api = useMemo(() => given ?? setupPacksApi(client as AdminAPIClient), [given, client]);
  const [mailboxes, setMailboxes] = useState<MailboxChoice[]>([]);
  const [context, setContext] = useState<SetupContext>(EMPTY);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [chosen, setChosen] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [stale, setStale] = useState(false);
  const [result, setResult] = useState<ApplyResult | null>(null);

  const show = useCallback((next: Plan) => {
    setPlan(next);
    const done = appliedKeys(next);
    setChosen(new Set(next.packs.flatMap((pack) => pack.items)
      .filter((item) => item.selected && applicable(item) && !done.has(item.key)).map((item) => item.key)));
  }, []);

  useEffect(() => {
    let live = true;
    api.latest().then((found) => {
      if (!live) return;
      setMailboxes(found.mailboxes);
      if (found.plan) {
        setContext(found.plan.context);
        show(found.plan);
      }
    }).catch((reason) => { if (live) setError(errorText(reason, 'Faxbot could not load its suggestions. Reload to try again.')); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [api, show]);

  const preview = async () => {
    setBusy(true);
    setError(null);
    setStale(false);
    setResult(null);
    try {
      show(await api.preview(context));
    } catch (reason) {
      setError(errorText(reason, 'Faxbot could not prepare suggestions. Try again in a minute.'));
    } finally {
      setBusy(false);
    }
  };

  const apply = async () => {
    if (!plan) return;
    setBusy(true);
    setError(null);
    try {
      const done = await api.apply(plan, [...chosen]);
      setResult(done);
      show(done.plan);
    } catch (reason) {
      setStale(reason instanceof AdminAPIError && reason.status === 409);
      setError(errorText(reason, 'Faxbot could not apply these suggestions. Try again in a minute.'));
    } finally {
      setBusy(false);
    }
  };

  const toggle = (key: string) => setChosen((current) => {
    const next = new Set(current);
    if (next.has(key)) next.delete(key); else next.add(key);
    return next;
  });

  const setMailboxCountry = (id: string, country: string) => setContext((current) => {
    const next = { ...current.mailboxes };
    if (country) next[id] = { country }; else delete next[id];
    return { ...current, mailboxes: next };
  });

  if (loading) {
    return <Box sx={{ display: 'flex', gap: 2, alignItems: 'center' }}><CircularProgress size={24} />
      <Typography>Loading suggestions…</Typography></Box>;
  }

  const applied = plan ? appliedKeys(plan) : new Set<string>();
  const replays = plan ? Object.values(plan.checks).map((check) => check.replay).filter(Boolean) as string[] : [];
  return (
    <Box>
      <Typography variant="h6" gutterBottom>Suggested Packs</Typography>
      <Typography variant="body2" sx={{ mb: 2 }}>
        Faxbot looks at your accounts, numbers, the faxes you sent and received, and your partners, and suggests
        rules and settings that save money and trouble. Previewing changes nothing.
      </Typography>

      <Paper variant="outlined" sx={{ p: 2, mb: 3 }}>
        <Typography variant="subtitle1" component="h3" sx={{ fontWeight: 600, mb: 1 }}>Describe your organization</Typography>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          Faxbot uses this only for its suggestions. A country you leave as not stated gets no country-specific
          suggestions.
        </Typography>
        <Stack spacing={2} sx={{ maxWidth: 520 }}>
          <TextField size="small" label="Business name" value={context.organization_name} disabled={!canEdit}
            inputProps={{ maxLength: 80 }}
            helperText="Printed at the top of each page you send. Faxbot never guesses it."
            onChange={(event) => setContext({ ...context, organization_name: event.target.value })} />
          <CountrySelect label="Country your organization works in" value={context.country} countries={countries}
            disabled={!canEdit} onChange={(country) => setContext({ ...context, country })} />
          {mailboxes.map((box) => (
            <CountrySelect key={box.id} label={`Country ${box.name} works in`}
              value={context.mailboxes[box.id]?.country ?? ''} countries={countries} disabled={!canEdit}
              helperText={box.numbers_country ? `Its numbers are in ${countryName(box.numbers_country)}.`
                : 'Leave it as not stated to use the organization’s country.'}
              onChange={(country) => setMailboxCountry(box.id, country)} />
          ))}
        </Stack>
        <Button variant="outlined" sx={{ mt: 2 }} disabled={!canEdit || busy} onClick={() => { void preview(); }}>
          {plan ? 'Preview again' : 'Preview suggestions'}
        </Button>
      </Paper>

      {error && <Alert severity={stale ? 'warning' : 'error'} sx={{ mb: 2 }}
        action={stale ? <Button color="inherit" size="small" onClick={() => { void preview(); }}>Preview again</Button> : undefined}>
        {error}
      </Alert>}
      {result && <Alert severity={result.outcome === 'applied' ? 'success' : 'warning'} sx={{ mb: 2 }}>
        <Typography variant="body2">{result.sentence}</Typography>
        {result.steps.map((step) => <Typography key={step.part} variant="body2">{step.sentence}</Typography>)}
      </Alert>}

      {plan && <>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          Suggested from what Faxbot knew on {formatServerTime(plan.created_at)}.
        </Typography>
        {replays.map((sentence) => <Alert key={sentence} severity="info" sx={{ mb: 2 }}>
          Checked against your recent faxes: {sentence}
        </Alert>)}
        {plan.packs.filter((pack) => pack.items.length > 0).map((pack) => (
          <Paper key={pack.key} variant="outlined" sx={{ p: 2, mb: 2 }} data-testid={`setup-pack-${pack.key}`}>
            <Typography variant="subtitle1" component="h3" sx={{ fontWeight: 600 }}>{pack.title}</Typography>
            <Typography variant="body2" color="text.secondary">{pack.sentence}</Typography>
            {pack.saving && <Typography variant="body2" sx={{ mt: 0.5 }}>
              The chosen suggestions save about {formatMoney(pack.saving)} a month (estimate).
            </Typography>}
            <Divider sx={{ mt: 1 }} />
            {pack.items.map((item) => <ItemRow key={item.key} item={item} chosen={chosen.has(item.key)}
              applied={applied.has(item.key)} canEdit={canEdit} onToggle={() => toggle(item.key)} onNavigate={onNavigate} />)}
          </Paper>
        ))}
        {plan.packs.every((pack) => pack.items.length === 0) && <Alert severity="info" sx={{ mb: 2 }}>
          Nothing to suggest yet. Suggestions come from the faxes you send and receive, so preview again after a few
          weeks of faxing.
        </Alert>}

        <Box sx={{ display: 'flex', alignItems: 'center', gap: 2, mb: 3, flexWrap: 'wrap' }}>
          <Button variant="contained" disabled={!canEdit || busy || chosen.size === 0} onClick={() => { void apply(); }}>
            {chosen.size === 1 ? 'Apply 1 chosen suggestion' : `Apply ${chosen.size} chosen suggestions`}
          </Button>
          <Typography variant="body2" color="text.secondary">
            Saves the chosen settings and publishes the chosen rules in one step. Steps marked “You do this” stay with you.
          </Typography>
        </Box>

        {plan.missing.length > 0 && (
          <Paper variant="outlined" sx={{ p: 2, mb: 2 }}>
            <Typography variant="subtitle1" component="h3" sx={{ fontWeight: 600, mb: 1 }}>What’s missing</Typography>
            {plan.missing.map((entry) => (
              <Box key={entry.key} sx={{ py: 1 }} data-testid={`setup-missing-${entry.key}`}>
                <Stack direction="row" spacing={1} sx={{ mb: 0.5, flexWrap: 'wrap', rowGap: 0.5 }}>
                  <Chip size="small" label={OWNER_LABEL[entry.owner]} variant="outlined" />
                  <Chip size="small" color={entry.status === 'blocking' ? 'warning' : 'default'}
                    label={entry.status === 'blocking' ? `Holds back: ${entry.operation}` : `Affects: ${entry.operation}`} />
                </Stack>
                <Typography variant="body2">{entry.sentence}</Typography>
                {entry.link && onNavigate && <Button size="small" sx={{ px: 0 }}
                  onClick={() => onNavigate(entry.link as AdminDestination)}>Open the page</Button>}
              </Box>
            ))}
          </Paper>
        )}

        {(plan.mailboxes.length > 0 || plan.workflows.length > 0) && (
          <Paper variant="outlined" sx={{ p: 2 }}>
            <Typography variant="subtitle1" component="h3" sx={{ fontWeight: 600, mb: 1 }}>Each mailbox</Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
              What each mailbox uses now, and where each setting comes from. Country-specific suggestions apply only
              to the mailboxes that work in that country.
            </Typography>
            {[...plan.mailboxes, ...plan.workflows].map((view) => (
              <Box key={'id' in view ? view.id : view.key} sx={{ mb: 2 }}>
                <Typography sx={{ fontWeight: 600 }}>{view.name}</Typography>
                <Table size="small" aria-label={`${view.name} settings`}>
                  <TableHead><TableRow><TableCell>Setting</TableCell><TableCell>Now</TableCell><TableCell>From</TableCell></TableRow></TableHead>
                  <TableBody>
                    {view.choices.map((choice) => <TableRow key={choice.label}>
                      <TableCell>{choice.label}</TableCell><TableCell>{choice.value}</TableCell><TableCell>{choice.source}</TableCell>
                    </TableRow>)}
                  </TableBody>
                </Table>
              </Box>
            ))}
          </Paper>
        )}
      </>}
    </Box>
  );
}
