// Try a fax: which route a fax would take under the rules in effect, your draft or an earlier version,
// and why, in one sentence. Nothing is sent and nothing is saved.
import { useEffect, useState } from 'react';
import {
  Accordion, AccordionDetails, AccordionSummary, Alert, Box, Button, Checkbox, FormControl, FormControlLabel, InputLabel,
  MenuItem, Paper, Select, Stack, Table, TableBody, TableCell, TableHead, TableRow, TextField, Typography,
} from '@mui/material';
import ExpandMoreIcon from '@mui/icons-material/ExpandMore';
import { DeliveryError, formatMoney, NOT_PRICED } from './delivery/shared';
import type {
  ExplainRequest, ExplainResult, Revision, RulesApi, RulesDocument, RulesState, Scope, StepResult, TraceStep,
} from './ProviderRulesApi';
import { CONDITION_FIELDS } from './ProviderRulesEditor';
import { layoutWords } from './ProviderRulesText';
import { documentLabels, scopeParam } from './ProviderRulesApi';

const SEVERITY = { route: 'success', held: 'warning', blocked: 'error' } as const;

const RESULT: Record<StepResult, string> = {
  matched: 'Matched', not_matched: 'Did not match', unless: 'Matched, but an exception applies',
  not_reached: 'Not read: an earlier rule chose', not_applied: 'Matched, but did not choose',
};
const NOTE: Record<string, string> = {
  mandatory: 'A mandatory rule chose instead.', overridden: 'A more specific rule chose instead.',
  excluded: 'Every account it names was left out by a limit.',
};
const SCOPE_NAME = { organization: 'Organization', mailbox: 'Mailbox', workflow: 'Workflow' } as const;

// The trace's "why" in words: the first condition that did not match, or why a match did not choose.
export function stepWhy(step: TraceStep): string {
  const parts: string[] = [];
  if (step.failed) {
    // The engine names the condition ("the recipient group"); a full sentence passes through as it is.
    parts.push(/[.!?]$/.test(step.failed) ? step.failed : `Its condition on ${step.failed} did not match.`);
  } else if (step.field) {
    const [block, key] = step.field.includes('.') ? step.field.split('.') : [null, step.field];
    const field = CONDITION_FIELDS.find((item) => item.block === block && item.key === key);
    parts.push(`${field?.label ?? 'A condition'} did not match.`);
  }
  if (step.note) parts.push(NOTE[step.note] ?? '');
  return parts.filter(Boolean).join(' ') || '-';
}

export function stepRule(step: TraceStep): string {
  if (step.rule_name) return step.rule_name;
  if (step.name) return step.name;
  return step.kind === 'preferred' ? "The recipient's preferred route" : '-';
}

function stepScope(step: TraceStep): string {
  const kind = step.scope.split(':')[0] as keyof typeof SCOPE_NAME;
  return step.scope_name ?? SCOPE_NAME[kind] ?? step.scope;
}

// "Every rule Faxbot read for this fax", folded away until opened.
export function TraceTable({ steps }: { steps: TraceStep[] }) {
  if (steps.length === 0) return null;
  return (
    <Accordion disableGutters variant="outlined" sx={{ borderRadius: 2 }}>
      <AccordionSummary expandIcon={<ExpandMoreIcon />}>
        <Typography variant="body2">Every rule Faxbot read for this fax</Typography>
      </AccordionSummary>
      <AccordionDetails sx={{ overflowX: 'auto' }}>
        <Table size="small" aria-label="Every rule Faxbot read">
          <TableHead><TableRow><TableCell>Rules</TableCell><TableCell>Rule</TableCell><TableCell>Result</TableCell>
            <TableCell>Why</TableCell></TableRow></TableHead>
          <TableBody>
            {steps.map((step, index) => (
              <TableRow key={`${step.scope}-${step.rule_id ?? step.kind}-${index}`}>
                <TableCell>{stepScope(step)}{step.revision ? `, version ${step.revision}` : ''}</TableCell>
                <TableCell>{stepRule(step)}</TableCell>
                <TableCell>{RESULT[step.result]}</TableCell>
                <TableCell>{stepWhy(step)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </AccordionDetails>
    </Accordion>
  );
}

export function ExplainAnswer({ result }: { result: ExplainResult }) {
  return (
    <Box data-testid="explain-answer">
      <Alert severity={SEVERITY[result.outcome]} sx={{ mb: 2 }}>{result.sentence}</Alert>
      {result.routes.length > 0 && (
        <Paper variant="outlined" sx={{ borderRadius: 2, mb: 2, overflowX: 'auto' }}>
          <Table size="small" aria-label="Routes in order">
            <TableHead>
              <TableRow><TableCell>Account</TableCell><TableCell>Price for this fax</TableCell><TableCell>Calls from</TableCell>
                <TableCell>Why</TableCell></TableRow>
            </TableHead>
            <TableBody>
              {result.routes.map((route) => (
                <TableRow key={route.account} sx={{ opacity: route.usable ? 1 : 0.6 }}>
                  <TableCell>{route.label}</TableCell>
                  <TableCell>{route.quote_text ?? (route.quote ? `About ${formatMoney(route.quote)}` : NOT_PRICED)}</TableCell>
                  <TableCell>{route.origin ?? '-'}</TableCell>
                  <TableCell>{route.sentence}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Paper>
      )}
      {result.holds.filter((hold) => hold !== result.sentence).map((hold) => (
        <Typography key={hold} variant="body2" sx={{ mb: 1 }}>{hold}</Typography>
      ))}
      {result.dial && <Typography variant="body2" sx={{ mb: 1 }}>{result.dial.sentence}</Typography>}
      {result.page_layout && (
        <Typography variant="body2" sx={{ mb: 1 }}>Pages per sheet: {layoutWords(result.page_layout)}.</Typography>
      )}
      {result.subaddress && (
        <Typography variant="body2" sx={{ mb: 1 }}>
          The fax asks for subaddress {result.subaddress} at the recipient's number.
        </Typography>
      )}
      <TraceTable steps={result.trace} />
    </Box>
  );
}

export default function ProviderRulesTry({ api, scope, state, document }: {
  api: RulesApi; scope: Scope; state: RulesState; document: RulesDocument;
}) {
  const [to, setTo] = useState('');
  const [pages, setPages] = useState('1');
  const [sizeMb, setSizeMb] = useState('');
  const [sender, setSender] = useState('me');
  const [mailbox, setMailbox] = useState('');
  const [workflow, setWorkflow] = useState('');
  const [labels, setLabels] = useState<string[]>([]);
  const [urgent, setUrgent] = useState(false);
  const [realCall, setRealCall] = useState(false);
  const [at, setAt] = useState('');
  const [source, setSource] = useState<string>(state.draft ? 'draft' : 'active');
  const [revisions, setRevisions] = useState<Revision[]>([]);
  const [result, setResult] = useState<ExplainResult | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let live = true;
    api.revisions(scope).then((value) => { if (live) setRevisions(value.revisions); }).catch(() => undefined);
    return () => { live = false; };
  }, [api, scope]);

  const run = async () => {
    setBusy(true);
    setError(null);
    const body: ExplainRequest = {
      to: to.trim(), pages: Number(pages) || 1, size_bytes: sizeMb ? Math.round(Number(sizeMb) * 1_000_000) : null,
      as: sender, mailbox: mailbox || null, workflow: workflow || null, urgent, real_call: realCall, labels,
      at: at || null, scope: scopeParam(scope),
      source: source === 'active' || source === 'draft' ? source : { revision: Number(source) },
    };
    try {
      setResult(await api.explain(body));
    } catch (failure) {
      setError(failure);
      setResult(null);
    } finally {
      setBusy(false);
    }
  };

  const select = (label: string, value: string, onChange: (value: string) => void, options: Array<[string, string]>) => (
    <FormControl size="small" sx={{ minWidth: 200 }}>
      <InputLabel id={`try-${label}`}>{label}</InputLabel>
      <Select labelId={`try-${label}`} label={label} value={value} inputProps={{ 'aria-label': label }}
        onChange={(event) => onChange(event.target.value)}>
        {options.map(([option, text]) => <MenuItem key={option} value={option}>{text}</MenuItem>)}
      </Select>
    </FormControl>
  );
  const allLabels = documentLabels(document);

  return (
    <Box>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
        See which route a fax would take, and why. Nothing is sent and nothing is saved.
      </Typography>
      <Paper variant="outlined" sx={{ p: 2, borderRadius: 2, mb: 2 }}>
        <Stack spacing={2}>
          <Stack direction={{ xs: 'column', md: 'row' }} spacing={2}>
            <TextField size="small" label="Fax number" value={to} onChange={(event) => setTo(event.target.value)} required />
            <TextField size="small" label="Pages" type="number" value={pages} inputProps={{ min: 1, max: 1000 }}
              onChange={(event) => setPages(event.target.value)} sx={{ width: 120 }} />
            <TextField size="small" label="File size (MB)" type="number" value={sizeMb} inputProps={{ min: 0, step: 0.1 }}
              onChange={(event) => setSizeMb(event.target.value)} sx={{ width: 160 }} />
            <TextField size="small" label={`Time at this installation (${state.time_zone})`} type="datetime-local" value={at}
              InputLabelProps={{ shrink: true }} helperText="When the fax is sent. Leave empty for now."
              onChange={(event) => setAt(event.target.value)} />
          </Stack>
          <Stack direction={{ xs: 'column', md: 'row' }} spacing={2} flexWrap="wrap" useFlexGap>
            {select('Sent by', sender, setSender, [['me', 'Me'], ...state.choices.people.map((person): [string, string] => [person.id, person.name])])}
            {select('From mailbox', mailbox, setMailbox, [['', 'No mailbox'], ...state.choices.mailboxes.map((item): [string, string] => [item.id, item.name])])}
            {select('Workflow', workflow, setWorkflow, [['', 'No workflow'], ...(document.workflows ?? []).map((item): [string, string] => [item.key, item.name])])}
            {select('Rules to try', source, setSource, [
              ['active', 'The rules in effect'],
              ...(state.draft ? [['draft', 'Your draft'] as [string, string]] : []),
              ...revisions.map((revision): [string, string] => [String(revision.number), `Version ${revision.number}`]),
            ])}
          </Stack>
          {allLabels.length > 0 && (
            <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap aria-label="Labels">
              {allLabels.map((label) => (
                <FormControlLabel key={label} label={label} control={(
                  <Checkbox size="small" checked={labels.includes(label)} onChange={(event) => setLabels(
                    event.target.checked ? [...labels, label] : labels.filter((item) => item !== label))} />
                )} />
              ))}
            </Stack>
          )}
          <Stack direction="row" spacing={2}>
            <FormControlLabel label="Marked urgent" control={<Checkbox checked={urgent} onChange={(event) => setUrgent(event.target.checked)} />} />
            <FormControlLabel label="Place a real call" control={<Checkbox checked={realCall} onChange={(event) => setRealCall(event.target.checked)} />} />
          </Stack>
          <Box><Button variant="contained" onClick={() => void run()} disabled={busy || !to.trim()}>Try it</Button></Box>
        </Stack>
      </Paper>
      <DeliveryError error={error} onClose={() => setError(null)} />
      {result && <ExplainAnswer result={result} />}
    </Box>
  );
}
