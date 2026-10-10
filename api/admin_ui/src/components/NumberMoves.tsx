import { useEffect, useState } from 'react';
import { Alert, Box, Button, Chip, Paper, Stack, TextField, Typography } from '@mui/material';
import AdminAPIClient, { accessErrorMessage } from '../api/client';

type Dependency = { question: string; label: string; answer: string; note: string | null };
type NumberAdvice = { number: string; display: string; sentence: string; verdict_label: string; reasons: string[];
  evidence: { last_arrival_text: string; removed_sentence: string }; dependencies: Dependency[] };
type Advice = { sentence: string; note: string; numbers: NumberAdvice[] };
type Choice = { key: string; label: string };
type Step = { step: string; label: string; state: string; state_label: string; evidence: string[];
  action: string | null; action_label: string | null };
type Plan = { number: string; state: string; sentence: string; note: string; steps: Step[];
  accounts: Choice[]; origins: Choice[] };
const RECORDED = new Set(['delivery', 'new_account_ready', 'new_route_tested', 'port_ordered', 'cutover']);

function DependencyAnswer({ value, canWrite, save }: { value: Dependency; canWrite: boolean;
  save: (question: string, answer: string, note: string) => Promise<void> }) {
  const [answer, setAnswer] = useState(value.answer);
  const [note, setNote] = useState(value.note ?? '');
  const [busy, setBusy] = useState(false);
  useEffect(() => { setAnswer(value.answer); setNote(value.note ?? ''); }, [value.answer, value.note]);
  if (!canWrite) return <Typography>{value.label} {value.answer === 'unknown' ? 'Not sure' : value.answer}{value.note ? ` — ${value.note}` : ''}</Typography>;
  return <Stack spacing={1}>
    <TextField select SelectProps={{ native: true }} label={value.label} value={answer}
      onChange={(event) => setAnswer(event.target.value)} disabled={busy}>
      <option value="unknown">Not sure</option><option value="yes">Yes</option><option value="no">No</option>
    </TextField>
    <TextField label={`Note: ${value.label}`} value={note} onChange={(event) => setNote(event.target.value)}
      inputProps={{ maxLength: 2000 }} disabled={busy} />
    <Button disabled={busy} onClick={async () => { setBusy(true); try { await save(value.question, answer, note); }
      finally { setBusy(false); } }}>Save answer</Button>
  </Stack>;
}

function MovePlan({ client, number, canWrite }: {client: AdminAPIClient; number: string; canWrite: boolean}) {
  const [plan, setPlan] = useState<Plan | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [account, setAccount] = useState('');
  const [origin, setOrigin] = useState('');
  const [note, setNote] = useState('');
  useEffect(() => { let live = true;
    client.call<Plan>({method: 'GET', path: `/routing/numbers/${encodeURIComponent(number)}/move`})
      .then((found) => {if (live) setPlan(found);}).catch((e) => {if (live) setError(accessErrorMessage(e));});
    return () => { live = false; };
  }, [client, number]);
  async function write(path: string, body?: unknown) {
    setBusy(true); setError('');
    try { setPlan(await client.call<Plan>({method: 'POST', path, body})); }
    catch (e) { setError(accessErrorMessage(e)); } finally { setBusy(false); }
  }
  const record = (step: string, state: string) => write(`/routing/numbers/${encodeURIComponent(number)}/move/steps/${encodeURIComponent(step)}`, {state, note});
  return <Stack spacing={2} sx={{mt: 2}}>
    {error && <Alert severity="error">{error}</Alert>}
    {!plan ? <Typography>Loading move plan…</Typography> : <>
      <Typography variant="h6">{plan.sentence}</Typography><Typography>{plan.note}</Typography>
      <Button disabled={busy} onClick={async () => { setBusy(true); try {
        setPlan(await client.call<Plan>({method: 'GET', path: `/routing/numbers/${encodeURIComponent(number)}/move`}));
      } catch (e) {setError(accessErrorMessage(e));} finally {setBusy(false);} }}>Refresh move evidence</Button>
      {canWrite && plan.state !== 'open' && <Stack spacing={1}>
        <TextField select SelectProps={{native: true}} label="Move to account" value={account} onChange={(e) => setAccount(e.target.value)}>
          <option value="">Choose a receiving account</option>{plan.accounts.map((choice) => <option key={choice.key} value={choice.key}>{choice.label}</option>)}
        </TextField>
        <Button disabled={busy || !account} onClick={() => write(`/routing/numbers/${encodeURIComponent(number)}/move`, {to_account: account})}>Start move plan</Button>
      </Stack>}
      {canWrite && plan.state === 'open' && <TextField label="Note for the next recorded step" value={note}
        onChange={(e) => setNote(e.target.value)} inputProps={{maxLength: 2000}} />}
      {plan.steps.map((step) => <Paper key={step.step} variant="outlined" sx={{p: 2}}>
        <Typography fontWeight={600}>{step.label} <Chip component="span" size="small" label={step.state_label}/></Typography>
        {step.evidence.map((line, index) => <Typography key={index}>{line}</Typography>)}
        {canWrite && plan.state === 'open' && RECORDED.has(step.step) && <Button disabled={busy}
          onClick={() => record(step.step, step.state === 'done' ? 'not_done' : 'done')}>
          {step.state === 'done' ? 'Mark not done' : step.action_label || 'Record it'}</Button>}
      </Paper>)}
      {canWrite && plan.state === 'open' && <>
        <TextField select SelectProps={{native: true}} label="Receipt test route" value={origin} onChange={(e) => setOrigin(e.target.value)}>
          <option value="">Choose a sending route</option>{plan.origins.map((choice) => <option key={choice.key} value={choice.key}>{choice.label}</option>)}
        </TextField>
        <Typography>Start watching, then send a test fax to this number through that route. Faxbot checks where it arrives.</Typography>
        <Button disabled={busy || !origin} onClick={() => write(`/routing/numbers/${encodeURIComponent(number)}/move/tests`, {origin})}>Watch for receipt test</Button>
        <Button disabled={busy} onClick={() => write(`/routing/numbers/${encodeURIComponent(number)}/move/forget`)}>Forget old carrier learning</Button>
        <Stack direction="row" spacing={1}>
          <Button disabled={busy || plan.steps.some((step) => step.state !== 'done')} onClick={() => record('move', 'finished')}>Finish move plan</Button>
          <Button disabled={busy} onClick={() => record('move', 'abandoned')}>Abandon move plan</Button>
        </Stack>
      </>}
    </>}
  </Stack>;
}

export default function NumberMoves({client, canWrite}: {client: AdminAPIClient; canWrite: boolean}) {
  const [advice, setAdvice] = useState<Advice | null>(null);
  const [error, setError] = useState('');
  const [selected, setSelected] = useState<string | null>(null);
  async function load() {setAdvice(await client.call<Advice>({method: 'GET', path: '/routing/recommendations/lines'}));}
  useEffect(() => { let live = true;
    client.call<Advice>({method: 'GET', path: '/routing/recommendations/lines'}).then((found) => {if (live) setAdvice(found);})
      .catch((e) => {if (live) setError(accessErrorMessage(e));});
    return () => {live = false;};
  }, [client]);
  async function answer(number: string, question: string, value: string, note: string) {
    setError('');
    try { await client.call({method: 'POST', path: `/routing/numbers/${encodeURIComponent(number)}/dependencies`, body: {question, answer: value, note}}); await load(); }
    catch (e) {setError(accessErrorMessage(e));}
  }
  return <Stack spacing={2}>
    <Typography variant="h4">Number moves</Typography>
    {error && <Alert severity="error">{error}</Alert>}
    {!advice ? <Typography>Loading number advice…</Typography> : <>
      <Typography>{advice.sentence}</Typography><Typography color="text.secondary">{advice.note}</Typography>
      <Button onClick={() => load().catch((e) => setError(accessErrorMessage(e)))}>Refresh advice</Button>
      {advice.numbers.map((row) => <Paper key={row.number} variant="outlined" sx={{p: 2}}>
        <Typography variant="h6">{row.display} <Chip component="span" size="small" label={row.verdict_label}/></Typography>
        <Typography>{row.sentence}</Typography>
        <Typography>{row.evidence.last_arrival_text}</Typography><Typography>{row.evidence.removed_sentence}</Typography>
        {row.reasons.map((line, index) => <Typography key={index}>{line}</Typography>)}
        <Stack spacing={2} sx={{my: 2}}>{row.dependencies.map((item) => <DependencyAnswer key={item.question} value={item}
          canWrite={canWrite} save={(question, value, note) => answer(row.number, question, value, note)}/>)}</Stack>
        <Button onClick={() => setSelected(selected === row.number ? null : row.number)}>Move plan</Button>
        {selected === row.number && <Box><MovePlan key={row.number} client={client} number={row.number} canWrite={canWrite}/></Box>}
      </Paper>)}
    </>}
  </Stack>;
}
