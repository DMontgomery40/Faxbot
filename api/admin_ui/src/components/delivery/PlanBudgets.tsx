// Costs → Prices & plans → Your plans this month: for each flat, allowance or committed plan, its normal-use budget
// or allowance, what is left until the counts start again, what is committed, the day-by-day burn-down, and whose
// bill a fax between your own accounts falls on. Every figure is an estimate. The budgets are the setting
// plan_budgets; Faxbot never changes a plan itself.
import { useCallback, useEffect, useState } from 'react';
import {
  Accordion, AccordionDetails, AccordionSummary, Box, Button, Chip, CircularProgress, LinearProgress, Paper, Stack,
  Table, TableBody, TableCell, TableContainer, TableHead, TableRow, TextField, Typography,
} from '@mui/material';
import ExpandMoreIcon from '@mui/icons-material/ExpandMore';
import AdminAPIClient from '../../api/client';
import type { PlanContract, PlanContracts } from '../../api/deliveryTypes';
import { formatLocalDate } from '../../api/time';
import { Field, FormDialog } from '../access/AccessViews';
import { DeliveryError, formatMoneyList } from './shared';

const STATE_LABELS: Record<PlanContract['state'], string> = {
  within: 'Within budget', over_budget: 'Over your normal-use budget', past_allowance: 'Past the allowance',
  no_limit: 'No budget set',
};

export const PLAN_KEYS = ['pages', 'faxes', 'day', 'included_pages', 'page_overage', 'included_minutes', 'commitment'] as const;
type PlanKey = typeof PLAN_KEYS[number];
export type PlanDraft = Record<PlanKey, string>;

const COUNT = /^\d{1,7}$/;
const PRICE = /^\d{1,5}(\.\d{1,6})?$/;

// The setting plan_budgets with one plan's entry replaced; null goes back to Faxbot's starting budget. The server
// checks the whole value and stores it in one order.
export function withPlanEntry(text: string, route: string, entry: string | null): string {
  const kept = text.split(';').map((part) => part.trim()).filter(Boolean)
    .filter((part) => part.split(':')[0].trim().toLowerCase() !== route);
  return [...kept, ...(entry === null ? [] : [`${route}:${entry}`])].join('; ');
}

// One plan's entry: an empty page or fax budget means no limit; empty allowance fields are left out.
export function planEntry(draft: PlanDraft): string {
  return PLAN_KEYS.flatMap((key) => {
    const value = draft[key].trim();
    if (key === 'pages' || key === 'faxes') return [`${key}=${value || 'none'}`];
    return value ? [`${key}=${value}`] : [];
  }).join(',');
}

export function planDraft(plan: PlanContract): PlanDraft {
  const { budget } = plan;
  const count = (value: number | null) => (value === null ? '' : String(value));
  return {
    pages: count(budget.pages), faxes: count(budget.faxes), day: String(budget.day),
    included_pages: count(budget.included_pages), page_overage: budget.page_overage[0]?.amount ?? '',
    included_minutes: count(budget.included_minutes), commitment: budget.commitment[0]?.amount ?? '',
  };
}

function draftProblem(draft: PlanDraft): string | null {
  for (const key of ['pages', 'faxes', 'included_pages', 'included_minutes'] as const) {
    const value = draft[key].trim();
    if (value && (!COUNT.test(value) || Number(value) < 1 || Number(value) > 1_000_000)) {
      return 'Budgets and allowances are whole numbers from 1 to 1,000,000; leave a field empty for none.';
    }
  }
  const day = Number(draft.day);
  if (!/^\d{1,2}$/.test(draft.day.trim()) || day < 1 || day > 31) return 'The billing day is a day of the month from 1 to 31.';
  for (const key of ['page_overage', 'commitment'] as const) {
    if (draft[key].trim() && !PRICE.test(draft[key].trim())) return 'Enter amounts as numbers, such as 0.10 or 50.';
  }
  return null;
}

function count(value: number | null | undefined): string {
  return value === null || value === undefined ? '-' : value.toLocaleString();
}

function plural(value: number, one: string, many = `${one}s`): string {
  return `${value.toLocaleString()} ${value === 1 ? one : many}`;
}

function rows(plan: PlanContract): Array<[string, string]> {
  const { budget, used, left, overage } = plan;
  const found: Array<[string, string]> = [
    ['Pages sent and received', count(used.pages)],
    ['Faxes sent and received', count(used.faxes)],
  ];
  if (budget.pages !== null || budget.faxes !== null) {
    const limit = [budget.pages !== null && plural(budget.pages, 'page'), budget.faxes !== null && plural(budget.faxes, 'fax', 'faxes')]
      .filter(Boolean).join(' and ');
    const rest = [left.pages !== null && plural(Math.max(0, left.pages), 'page'),
      left.faxes !== null && plural(Math.max(0, left.faxes), 'fax', 'faxes')].filter(Boolean).join(' and ');
    found.push(['Normal-use budget a month', limit], ['Left of the budget', rest]);
  }
  if (budget.included_pages) {
    found.push(['Pages the plan includes', count(budget.included_pages)],
      ['Included pages left', count(Math.max(0, left.allowance ?? 0))],
      ['Price of each extra page', formatMoneyList(budget.page_overage, 'Not known')]);
  }
  if (budget.included_minutes) {
    found.push(['Minutes the plan includes', count(budget.included_minutes)], ['Minutes used', count(used.minutes)]);
  }
  if (budget.commitment.length) {
    found.push(['Monthly commitment', formatMoneyList(budget.commitment)],
      ['Spent so far', formatMoneyList(used.spend, 'Not known')]);
  }
  if (overage.pages || overage.minutes) {
    found.push(['Past the allowance so far', overage.cost_unknown ? 'Not known' : formatMoneyList(overage.cost)]);
  }
  found.push(['Committed this period', formatMoneyList(plan.committed, 'Nothing')],
    ['Bill so far', formatMoneyList(plan.bill_so_far, 'Not known')],
    ['Counts start again', formatLocalDate(plan.period.next_day)]);
  return found;
}

function Progress({ plan }: { plan: PlanContract }) {
  const limit = plan.budget.included_pages ?? plan.budget.pages;
  if (!limit) return null;
  const used = plan.used.pages;
  const what = plan.budget.included_pages ? 'included pages' : 'pages of your normal-use budget';
  return (
    <Box sx={{ my: 1.5 }}>
      <LinearProgress variant="determinate" value={Math.min(100, (used / limit) * 100)}
        color={used >= limit ? 'warning' : 'primary'} aria-label={`${plan.name} pages used this period`} />
      <Typography variant="caption" color="text.secondary" data-testid="plan-budget-progress">
        {`${used.toLocaleString()} of ${limit.toLocaleString()} ${what} used`}
      </Typography>
    </Box>
  );
}

function BudgetDialog({ plan, text, busy, error, onClose, onSave }: {
  plan: PlanContract;
  text: string;
  busy: boolean;
  error: unknown;
  onClose: () => void;
  onSave: (value: string) => void;
}) {
  const [draft, setDraft] = useState<PlanDraft>(() => planDraft(plan));
  const set = (key: PlanKey) => (value: string) => setDraft((current) => ({ ...current, [key]: value }));
  const problem = draftProblem(draft);
  const currency = plan.currency;
  return (
    <FormDialog open title={`${plan.name} budget`} submitLabel="Save" busy={busy} error={null}
      canSubmit={!problem} onSubmit={() => onSave(withPlanEntry(text, plan.route, planEntry(draft)))} onClose={onClose}>
      <DeliveryError error={error} />
      <Typography variant="body2" color="text.secondary">
        A normal-use budget is your own limit for a plan the provider calls unlimited; past it, Faxbot treats the plan
        as used up until the billing day. Faxbot never changes the plan itself.
      </Typography>
      <Box display="grid" gridTemplateColumns={{ xs: '1fr', sm: '1fr 1fr 1fr' }} columnGap={2}>
        <Field label="Pages a month" value={draft.pages} onChange={set('pages')} helperText="Empty for no limit." />
        <Field label="Faxes a month" value={draft.faxes} onChange={set('faxes')} helperText="Empty for no limit." />
        <TextField select fullWidth margin="normal" label="Billing day" value={draft.day}
          onChange={(event) => set('day')(event.target.value)} SelectProps={{ native: true }}
          InputLabelProps={{ shrink: true }} helperText="When the counts start again.">
          {Array.from({ length: 31 }, (_, index) => String(index + 1)).map((day) => (
            <option key={day} value={day}>{day}</option>
          ))}
        </TextField>
      </Box>
      <Typography variant="subtitle2" sx={{ mt: 2 }}>Allowance and commitment</Typography>
      <Typography variant="body2" color="text.secondary">
        For a plan that includes pages or minutes, or a monthly amount you have agreed to spend.
      </Typography>
      <Box display="grid" gridTemplateColumns={{ xs: '1fr', sm: '1fr 1fr' }} columnGap={2}>
        <Field label="Pages included" value={draft.included_pages} onChange={set('included_pages')} />
        <Field label={`Price of each extra page (${currency})`} value={draft.page_overage} onChange={set('page_overage')} />
        <Field label="Minutes included" value={draft.included_minutes} onChange={set('included_minutes')}
          helperText="Extra minutes cost the plan's price a minute." />
        <Field label={`Monthly commitment (${currency})`} value={draft.commitment} onChange={set('commitment')} />
      </Box>
      {problem && <Typography variant="body2" color="error" data-testid="plan-budget-problem">{problem}</Typography>}
    </FormDialog>
  );
}

function Plan({ plan, canWrite, onEdit, onDefault, busy }: {
  plan: PlanContract;
  canWrite: boolean;
  onEdit: () => void;
  onDefault: () => void;
  busy: boolean;
}) {
  const fee = formatMoneyList(plan.monthly_fee, '');
  const warn = plan.state === 'over_budget' || plan.state === 'past_allowance';
  return (
    <Paper variant="outlined" sx={{ p: 2, borderRadius: 2 }} data-testid="plan-budget">
      <Box display="flex" alignItems="center" gap={1} flexWrap="wrap" sx={{ mb: 1 }}>
        <Typography variant="h6" component="h3">{fee ? `${plan.name}, ${fee} a month` : plan.name}</Typography>
        <Chip size="small" label={STATE_LABELS[plan.state]} color={warn ? 'warning' : 'default'} />
        <Chip size="small" variant="outlined" label="Estimate" />
      </Box>
      <Typography variant="body1" data-testid="plan-budget-sentence">{plan.sentence}</Typography>
      <Progress plan={plan} />
      <TableContainer>
        <Table size="small" aria-label={`${plan.name} this billing period`}>
          <TableHead>
            <TableRow>
              <TableCell>Estimate</TableCell>
              <TableCell align="right">This period</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {rows(plan).map(([label, value]) => (
              <TableRow key={label}>
                <TableCell>{label}</TableCell>
                <TableCell align="right">{value}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableContainer>
      <Stack spacing={0.5} sx={{ mt: 1.5 }}>
        {[plan.budget.sentence, plan.pace_sentence, plan.bill_sentence, plan.count_sentence, plan.untimed_sentence].filter(Boolean).map((sentence) => (
          <Typography key={sentence} variant="body2" color="text.secondary">{sentence}</Typography>
        ))}
      </Stack>
      {plan.own_accounts.length > 0 && (
        <Box sx={{ mt: 1.5 }} data-testid="plan-own-accounts">
          <Typography variant="subtitle2">Between your own accounts</Typography>
          <Box component="ul" sx={{ mt: 0.5, mb: 0, pl: 3 }}>
            {plan.own_accounts.map((row) => (
              <Typography key={row.sentence} component="li" variant="body2">{row.sentence}</Typography>
            ))}
          </Box>
        </Box>
      )}
      {plan.burn_down.length > 0 && (
        <Accordion disableGutters variant="outlined" sx={{ mt: 1.5, borderRadius: 1 }}>
          <AccordionSummary expandIcon={<ExpandMoreIcon />}>
            <Typography variant="body2">Day by day since {formatLocalDate(plan.period.first_day)}</Typography>
          </AccordionSummary>
          <AccordionDetails>
            <Table size="small" aria-label={`${plan.name} day by day`}>
              <TableHead>
                <TableRow>
                  <TableCell>Day</TableCell>
                  <TableCell align="right">Pages so far</TableCell>
                  <TableCell align="right">Faxes so far</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {plan.burn_down.map((row) => (
                  <TableRow key={row.date}>
                    <TableCell>{formatLocalDate(row.date)}</TableCell>
                    <TableCell align="right">{row.pages.toLocaleString()}</TableCell>
                    <TableCell align="right">{row.faxes.toLocaleString()}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </AccordionDetails>
        </Accordion>
      )}
      {canWrite && (
        <Box sx={{ mt: 1.5, display: 'flex', gap: 1, flexWrap: 'wrap' }}>
          <Button size="small" variant="outlined" onClick={onEdit} aria-label={`Change the ${plan.name} budget`}>
            Change budget
          </Button>
          {plan.budget.source === 'set' && (
            <Button size="small" onClick={onDefault} disabled={busy}>Use Faxbot&apos;s starting budget</Button>
          )}
        </Box>
      )}
    </Paper>
  );
}

export default function PlanBudgets({ client, canWrite }: { client: AdminAPIClient; canWrite: boolean }) {
  const [data, setData] = useState<PlanContracts | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [editing, setEditing] = useState<PlanContract | null>(null);
  const [busy, setBusy] = useState(false);
  const [saveError, setSaveError] = useState<unknown>(null);

  const load = useCallback(async () => {
    try {
      setData(await client.getPlans());
      setError(null);
    } catch (failure) {
      setError(failure);
    }
  }, [client]);

  useEffect(() => { void load(); }, [load]);

  const save = async (value: string) => {
    setBusy(true);
    setSaveError(null);
    try {
      const current = await client.getSettings();
      const revision = current._meta?.desired_revision_id;
      if (!revision) throw new Error('Settings could not be loaded.');
      await client.updateSettings({ expected_revision_id: revision, plan_budgets: value });
      setEditing(null);
      await load();
    } catch (failure) {
      setSaveError(failure);
      if (!editing) setError(failure);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Box component="section" mt={4} data-testid="plan-budgets">
      <Typography variant="h6" component="h2">Your plans this month</Typography>
      <Typography variant="body2" color="text.secondary" mb={2}>
        What each plan has carried since its billing day, against your budget or its allowance. Every figure is an estimate.
      </Typography>
      <DeliveryError error={error} onClose={() => setError(null)} />
      {!data && !error && <CircularProgress size={24} />}
      {data && data.plans.length === 0 && (
        <Paper variant="outlined" sx={{ p: 2, borderRadius: 2 }}>
          <Typography variant="body1" data-testid="plan-budgets-empty">{data.empty_sentence}</Typography>
        </Paper>
      )}
      {data && data.plans.length > 0 && (
        <Stack spacing={2}>
          {data.plans.map((plan) => (
            <Plan key={plan.route} plan={plan} canWrite={canWrite} busy={busy}
              onEdit={() => { setSaveError(null); setEditing(plan); }}
              onDefault={() => void save(withPlanEntry(data.plan_budgets, plan.route, null))} />
          ))}
        </Stack>
      )}
      {editing && data && (
        <BudgetDialog plan={editing} text={data.plan_budgets} busy={busy} error={saveError}
          onClose={() => setEditing(null)} onSave={(value) => void save(value)} />
      )}
    </Box>
  );
}
