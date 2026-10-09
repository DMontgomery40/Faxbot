import { useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import {
  Alert, Box, Button, Checkbox, Chip, CircularProgress, FormControlLabel, MenuItem, Paper, Stack, TextField,
  Typography,
} from '@mui/material';
import AdminAPIClient, { AdminAPIError, accessErrorMessage } from '../../api/client';
import type { PortfolioGroup, PortfolioInput, PortfolioItem, PortfolioPlan, PortfolioResult } from '../../api/portfolioTypes';
import { toMicros } from './shared';

type ItemDraft = Omit<PortfolioItem, 'cost' | 'group_id'> & { cost: string; group_id: string };
type GroupDraft = Omit<PortfolioGroup, 'cost'> & { cost: string };
type RelationshipDraft = { id: string; a: string; b: string; expected: string; cautious: string };
type Computed = { input: PortfolioInput; result: PortfolioResult };
const MAX_AMOUNT = 1_000_000_000_000_000_000n;
const fields = { display: 'grid', gridTemplateColumns: { xs: '1fr', sm: 'repeat(2, minmax(0, 1fr))' }, gap: 2 };
const amount = (value: string) => value.trim() || null;
// Do not turn exact server decimals into Number or round away small net benefits.
const money = (currency: string, value: string | null) => value === null ? 'Unknown' : `${currency} ${value}`;

function MoneyField({ label, value, onChange, helperText }: {
  label: string; value: string; onChange: (value: string) => void; helperText?: string;
}) {
  return <TextField label={label} value={value} onChange={event => onChange(event.target.value)} size="small"
    inputProps={{ inputMode: 'decimal', maxLength: 21 }} helperText={helperText ?? 'Blank means unknown; enter 0 if none.'} />;
}

function Row({ title, children, remove, removeLabel }: {
  title: string; children: ReactNode; remove: () => void; removeLabel: string;
}) {
  return <Box component="fieldset" sx={{ border: 1, borderColor: 'divider', borderRadius: 2, p: 2, minWidth: 0, m: 0 }}>
    <Typography component="legend" variant="subtitle2" sx={{ px: 0.5 }}>{title}</Typography>
    <Box sx={fields}>{children}</Box>
    <Button size="small" onClick={remove} sx={{ mt: 1 }}>{removeLabel}</Button>
  </Box>;
}

function missingLabel(field: string, input: PortfolioInput): string {
  if (field === 'budget') return 'Budget';
  const match = /^(nodes|groups|relationships)\[(\d+)\]\.(\w+)$/.exec(field);
  if (!match) return 'Planning input';
  const index = Number(match[2]);
  if (match[1] === 'nodes') return input.nodes[index]?.label || 'Setup item';
  if (match[1] === 'groups') return input.groups[index]?.label || 'Shared cost';
  const relation = input.relationships[index];
  const name = (id: string) => input.nodes.find(node => node.id === id)?.label || 'Setup item';
  return relation ? `${name(relation.a)} and ${name(relation.b)}` : 'Relationship';
}

function Bundle({ title, plan, input }: { title: string; plan: PortfolioPlan; input: PortfolioInput }) {
  const newItems = input.nodes.filter(node => plan.new_ids.includes(node.id));
  const installed = input.nodes.filter(node => plan.installed_ids.includes(node.id));
  const paidGroups = new Set(input.nodes.filter(node => node.installed).map(node => node.group_id));
  const newGroups = input.groups.filter(group => !paidGroups.has(group.id)
    && newItems.some(node => node.group_id === group.id));
  const amounts: Array<[string, string]> = [
    ['Additional setup cost', plan.incremental_cost], ['Expected benefit', plan.expected_benefit],
    ['Cautious benefit', plan.cautious_benefit], ['Expected net benefit', plan.expected_net],
    ['Cautious net benefit', plan.cautious_net],
  ];
  return <Paper component="section" aria-label={title} variant="outlined" sx={{ p: 2, borderRadius: 2, minWidth: 0 }}>
    <Typography variant="h6" component="h3">{title}</Typography>
    <Typography variant="body2" color="text.secondary" mb={1}>
      {title === 'Expected plan' ? 'Highest expected net benefit.' : 'Best lower net benefit across the two supplied scenarios.'}
    </Typography>
    {plan.state === 'abstain' && <Alert severity="info">No additional setup: keep what is already installed.</Alert>}
    {installed.length > 0 && <Typography variant="body2">Already installed: {installed.map(node => node.label).join(', ')}.</Typography>}
    {newItems.length > 0 && <>
      <Typography variant="subtitle2" mt={1}>Items to add</Typography>
      <Box component="ul" sx={{ mt: 0.5, pl: 2.5 }}>
        {newItems.map(node => <li key={node.id}>{node.label} — {money(input.currency, node.cost)}</li>)}
      </Box>
      <Typography variant="subtitle2">Shared setup to add (charged once)</Typography>
      {newGroups.length ? <Box component="ul" sx={{ mt: 0.5, pl: 2.5 }}>
        {newGroups.map(group => <li key={group.id}>{group.label} — {money(input.currency, group.cost)}</li>)}
      </Box> : <Typography variant="body2">None. Any selected shared setup is already covered by an installed item.</Typography>}
    </>}
    <Box component="dl" sx={{ m: 0, mt: 1 }}>
      {amounts.map(([label, value]) => <Box key={label} sx={{ display: 'flex', flexWrap: 'wrap', gap: 1, justifyContent: 'space-between' }}>
        <Typography component="dt" variant="body2">{label}</Typography>
        <Typography component="dd" variant="body2" sx={{ m: 0 }}>{money(input.currency, value)}</Typography>
      </Box>)}
    </Box>
  </Paper>;
}

export default function PortfolioPlanner({ client }: { client: AdminAPIClient }) {
  const [currency, setCurrency] = useState('');
  const [horizon, setHorizon] = useState('');
  const [perspective, setPerspective] = useState('');
  const [budget, setBudget] = useState('');
  const [nodes, setNodes] = useState<ItemDraft[]>([]);
  const [groups, setGroups] = useState<GroupDraft[]>([]);
  const [relationships, setRelationships] = useState<RelationshipDraft[]>([]);
  const [computed, setComputed] = useState<Computed | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const sequence = useRef(0);
  const generation = useRef(0);
  const nextId = (prefix: string) => `${prefix}_${++sequence.current}`;
  const changed = () => { generation.current++; setComputed(null); setError(null); setBusy(false); };
  useEffect(() => {
    changed();
    return () => { generation.current++; };
  }, [client]);
  const updateNode = (id: string, patch: Partial<ItemDraft>) => { changed(); setNodes(rows => rows.map(row => row.id === id ? { ...row, ...patch } : row)); };
  const updateGroup = (id: string, patch: Partial<GroupDraft>) => { changed(); setGroups(rows => rows.map(row => row.id === id ? { ...row, ...patch } : row)); };
  const updateRelationship = (id: string, patch: Partial<RelationshipDraft>) => {
    changed(); setRelationships(rows => rows.map(row => row.id === id ? { ...row, ...patch } : row));
  };
  const compare = async () => {
    setComputed(null); setError(null);
    if (!/^[A-Z]{3}$/.test(currency.trim().toUpperCase()) || !horizon.trim() || !perspective.trim()
      || nodes.some(node => !node.label.trim()) || groups.some(group => !group.label.trim())) {
      setError('Enter a three-letter currency, a planning period, whose costs count, and a name for every item and shared cost.'); return;
    }
    const validAmount = (value: string, signed: boolean) => {
      if (!value.trim()) return true;
      if (!(signed ? /^-?\d{1,13}(?:\.\d{1,6})?$/ : /^\d{1,13}(?:\.\d{1,6})?$/).test(value.trim())) return false;
      const micros = toMicros(value);
      return micros !== null && micros <= MAX_AMOUNT && micros >= (signed ? -MAX_AMOUNT : 0n);
    };
    if ([budget, ...nodes.map(node => node.cost), ...groups.map(group => group.cost)].some(value => !validAmount(value, false))) {
      setError('Use nonnegative amounts up to 1,000,000,000,000 with at most six decimal places, or leave them blank for unknown.'); return;
    }
    if (relationships.some(row => !validAmount(row.expected, true) || !validAmount(row.cautious, true))) {
      setError('Benefits must be decimal amounts between -1,000,000,000,000 and 1,000,000,000,000 with at most six decimal places, or blank for unknown.'); return;
    }
    const pairs = new Set<string>();
    for (const row of relationships) {
      const pair = [row.a, row.b].sort().join(':');
      if (!row.a || !row.b || row.a === row.b || pairs.has(pair)) {
        setError('Choose two different items for each relationship and enter each pair only once.'); return;
      }
      pairs.add(pair);
    }
    const input: PortfolioInput = { currency: currency.trim().toUpperCase(), horizon: horizon.trim(), perspective: perspective.trim(),
      budget: amount(budget), nodes: nodes.map(row => ({ ...row, label: row.label.trim(), cost: amount(row.cost), group_id: row.group_id || null })),
      groups: groups.map(row => ({ ...row, label: row.label.trim(), cost: amount(row.cost) })),
      relationships: relationships.map(({ a, b, expected, cautious }) => ({ a, b, expected: amount(expected), cautious: amount(cautious) })) };
    const request = ++generation.current;
    setBusy(true);
    try {
      const result = await client.call<PortfolioResult>({ method: 'POST', path: '/routing/portfolio/plan', body: input });
      if (request === generation.current) setComputed({ input, result });
    } catch (failure) {
      if (request === generation.current) setError(failure instanceof AdminAPIError && [401, 403, 422].includes(failure.status)
        ? accessErrorMessage(failure) : "Faxbot couldn't compare these plans. Check the connection and try again.");
    } finally {
      if (request === generation.current) setBusy(false);
    }
  };
  return <Box component="section" aria-label="Setup planner" mt={4}>
    <Stack direction="row" spacing={1} alignItems="center" mb={1}>
      <Typography variant="h5" component="h2">Compare setup plans</Typography><Chip size="small" label="Estimate" variant="outlined" />
    </Stack>
    <Typography variant="body2" mb={1}>Compare up to ten setup items together, including benefits that need both ends of a connection.</Typography>
    <Typography variant="body2" color="text.secondary" mb={1}>
      Use one currency and the same planning period for every amount. Count costs and benefits only for the people or company you name.
      A payment transferred between them is not a second saving. Enter each relationship once; omitted relationships contribute no benefit.
    </Typography>
    <Typography variant="body2" color="text.secondary" mb={2}>
      Already installed items stay in every plan. Their setup costs, shared setup already in use, and benefits between installed items are not counted again.
      These estimates do not confirm recipient agreement or compatibility, enroll partners, or change routes.
    </Typography>
    <Typography variant="body2" fontWeight={600} mb={2}>Inputs are not saved.</Typography>
    <Box component="form" noValidate onSubmit={event => { event.preventDefault(); void compare(); }}>
      <Stack spacing={2}>
        <Box sx={fields}>
          <TextField label="Whose costs and benefits?" size="small" value={perspective} inputProps={{ maxLength: 200 }}
            onChange={event => { changed(); setPerspective(event.target.value); }} helperText="Name the company or people whose amounts you are comparing." />
          <TextField label="Planning period" size="small" value={horizon} inputProps={{ maxLength: 200 }}
            onChange={event => { changed(); setHorizon(event.target.value); }} helperText="State the period covered by every cost and benefit." />
          <TextField label="Currency" size="small" value={currency} inputProps={{ maxLength: 3 }}
            onChange={event => { changed(); setCurrency(event.target.value); }} helperText="Enter a three-letter currency code. No currency conversion is applied." />
          <MoneyField label="Budget" value={budget} onChange={value => { changed(); setBudget(value); }}
            helperText="Maximum additional setup spending. Blank means unknown; enter 0 for no spending." />
        </Box>
        <Typography variant="h6" component="h3">Shared setup costs</Typography>
        <Typography variant="body2">Add a cost here when several items use the same setup. It is charged once, in addition to each item&apos;s own cost.</Typography>
        {groups.map((group, index) => <Row key={group.id} title={`Shared cost ${index + 1}`} removeLabel="Remove shared cost" remove={() => {
          changed(); setGroups(rows => rows.filter(row => row.id !== group.id));
          setNodes(rows => rows.map(row => row.group_id === group.id ? { ...row, group_id: '' } : row));
        }}>
          <TextField label="Name" size="small" value={group.label} inputProps={{ maxLength: 100 }} onChange={event => updateGroup(group.id, { label: event.target.value })} />
          <MoneyField label="Shared setup cost" value={group.cost} onChange={cost => updateGroup(group.id, { cost })} />
        </Row>)}
        <Box><Button onClick={() => { changed(); setGroups(rows => [...rows, { id: nextId('shared'), label: '', cost: '' }]); }} disabled={groups.length >= 10}>Add shared cost</Button></Box>
        <Typography variant="h6" component="h3">Setup items</Typography>
        {nodes.length === 0 && <Alert severity="info">Add the setup items you want to compare. No prices or benefits have been assumed.</Alert>}
        {nodes.map((node, index) => <Row key={node.id} title={`Setup item ${index + 1}`} removeLabel="Remove setup item" remove={() => {
          changed(); setNodes(rows => rows.filter(row => row.id !== node.id));
          setRelationships(rows => rows.filter(row => row.a !== node.id && row.b !== node.id));
        }}>
          <TextField label="Name" size="small" value={node.label} inputProps={{ maxLength: 100 }} onChange={event => updateNode(node.id, { label: event.target.value })} />
          <MoneyField label="Setup cost" value={node.cost} onChange={cost => updateNode(node.id, { cost })}
            helperText={node.installed ? 'Already installed: this cost is not charged again.' : 'Exclude shared setup entered above. Blank means unknown.'} />
          <TextField label="Shared cost" select size="small" value={node.group_id} onChange={event => updateNode(node.id, { group_id: event.target.value })}>
            <MenuItem value="">None</MenuItem>
            {groups.map((group, i) => <MenuItem key={group.id} value={group.id}>{group.label || `Shared cost ${i + 1}`}</MenuItem>)}
          </TextField>
          <FormControlLabel label="Already installed" control={<Checkbox checked={node.installed} onChange={event => updateNode(node.id, { installed: event.target.checked })} />} />
        </Row>)}
        <Box><Button onClick={() => { changed(); setNodes(rows => [...rows, { id: nextId('item'), label: '', cost: '', installed: false, group_id: '' }]); }} disabled={nodes.length >= 10}>Add setup item</Button></Box>
        <Typography variant="h6" component="h3">Benefits between items</Typography>
        <Typography variant="body2">For each pair, enter the net reduction in recurring costs over your planning period when both items are available. Exclude setup costs entered above. A negative benefit means increased recurring costs. Use a cautious scenario as well as your expected one.</Typography>
        {relationships.map((row, index) => <Row key={row.id} title={`Relationship ${index + 1}`} removeLabel="Remove relationship" remove={() => {
          changed(); setRelationships(rows => rows.filter(item => item.id !== row.id));
        }}>
          {(['a', 'b'] as const).map((side, i) => <TextField key={side} label={i === 0 ? 'First item' : 'Second item'} select size="small" value={row[side]}
            onChange={event => updateRelationship(row.id, { [side]: event.target.value })}>
            <MenuItem value="">Choose an item</MenuItem>
            {nodes.map((node, n) => <MenuItem key={node.id} value={node.id}>{node.label || `Setup item ${n + 1}`}</MenuItem>)}
          </TextField>)}
          <MoneyField label="Expected benefit" value={row.expected} onChange={expected => updateRelationship(row.id, { expected })} />
          <MoneyField label="Cautious benefit" value={row.cautious} onChange={cautious => updateRelationship(row.id, { cautious })} />
        </Row>)}
        <Box><Button disabled={nodes.length < 2 || relationships.length >= 45} onClick={() => {
          changed(); setRelationships(rows => [...rows, { id: nextId('pair'), a: '', b: '', expected: '', cautious: '' }]);
        }}>Add relationship</Button></Box>
        {error && <Alert severity="error">{error}</Alert>}
        <Stack direction="row" spacing={2} alignItems="center">
          <Button type="submit" variant="contained" disabled={!nodes.length || busy}>Compare setup plans</Button>
          {busy && <CircularProgress size={24} aria-label="Comparing setup plans" />}
        </Stack>
      </Stack>
    </Box>
    {computed && <Box aria-live="polite" mt={3}>
      {computed.result.state === 'incomplete' ? <Alert severity="info">
        More information is needed before these plans can be compared.
        <Box component="ul" sx={{ pl: 2.5, mb: 0 }}>{computed.result.missing.map((missing, index) =>
          <li key={index}>{missingLabel(missing.field, computed.input)}: {missing.reason}</li>)}</Box>
      </Alert> : computed.result.plans && <>
        <Typography variant="body2" mb={1}>{computed.input.perspective} · {computed.input.horizon} · Budget {money(computed.input.currency, computed.result.budget)}</Typography>
        <Box sx={fields}>
          <Bundle title="Expected plan" plan={computed.result.plans.expected} input={computed.input} />
          <Bundle title="Cautious plan" plan={computed.result.plans.cautious} input={computed.input} />
        </Box>
      </>}
      <Typography variant="body2" mt={1}>{computed.result.note}</Typography>
      {computed.result.assumptions.map((line, index) => <Typography key={index} variant="body2" color="text.secondary">{line}</Typography>)}
    </Box>}
  </Box>;
}
