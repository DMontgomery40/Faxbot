// Savings & optimization → Opportunities → Receiving: which numbers could share incoming lines, which numbers get few calls, and what
// each fax service costs a month. Every figure is an estimate from past calls, shown next to its time window; Faxbot
// recommends only and never changes a carrier account.
import { useCallback, useEffect, useState } from 'react';
import {
  Accordion, AccordionDetails, AccordionSummary, Box, Chip, CircularProgress, Link, Paper, Stack, Table, TableBody,
  TableCell, TableContainer, TableHead, TableRow, Typography,
} from '@mui/material';
import ExpandMoreIcon from '@mui/icons-material/ExpandMore';
import AdminAPIClient from '../../api/client';
import type { ReceivingCosts, ReceivingNumber, ReceivingRecommendations as Advice } from '../../api/deliveryTypes';
import { parseServerTime } from '../../api/time';
import { DeliveryError, NOT_PRICED, formatMoneyList } from './shared';

const TOO_LITTLE = new Set(['too_little_history', 'no_trunk', 'no_channel_price']);

// A window's dates in the reader's own words: "6 September 2026 and 5 October 2026".
export function windowText(window: { start: string; end: string }): string {
  const day = (value: string) => parseServerTime(value)?.toLocaleDateString(undefined,
    { day: 'numeric', month: 'long', year: 'numeric' }) ?? '';
  return `${day(window.start)} and ${day(window.end)}`;
}

// A time a shared-line stretch began or ended, in local time.
export function momentText(value: string): string {
  return parseServerTime(value)?.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }) ?? '';
}

// The date a published price was read ("2026-10-05"), as a local calendar day.
export function readOnText(day: string | null): string {
  if (!day || !/^\d{4}-\d{2}-\d{2}$/.test(day)) return '';
  return new Date(`${day}T12:00:00`).toLocaleDateString(undefined, { day: 'numeric', month: 'long', year: 'numeric' });
}

export function adviceText(row: ReceivingNumber): string {
  if (!row.eligible) return row.reason ?? 'Billed by the minute';
  return row.in_pool ? 'Shared lines' : 'Billed by the minute';
}

const COST_ROWS: Array<[string, keyof ReceivingCosts]> = [
  ['Billed by the minute today', 'billed_by_the_minute'],
  ['Shared lines', 'channels'],
  ['Still billed by the minute', 'still_billed_by_the_minute'],
  ['Fax number rental', 'number_rental'],
  ['Total today', 'total_today'],
  ['Total with shared lines', 'total_with_pool'],
];

function Heading({ title }: { title: string }) {
  return (
    <Box display="flex" alignItems="center" gap={1} sx={{ mb: 1 }}>
      <Typography variant="h6" component="h3">{title}</Typography>
      <Chip size="small" variant="outlined" label="Estimate" />
    </Box>
  );
}

function SharedLines({ advice }: { advice: Advice }) {
  const { pool, days, windows } = advice;
  const pooled = (pool.pool_numbers ?? []).length > 0;
  // An empty figure is unknown: "Not priced yet", never $0.
  const figure = (values: ReceivingCosts[keyof ReceivingCosts] | undefined) => formatMoneyList(values, NOT_PRICED);
  return (
    <Paper variant="outlined" sx={{ p: 2, borderRadius: 2 }} data-testid="receiving-lines">
      <Heading title="Shared lines for received faxes" />
      <Typography variant="body1" data-testid="receiving-sentence">{pool.sentence}</Typography>
      {pool.action && <Typography variant="body2" sx={{ mt: 1 }} data-testid="receiving-action">{pool.action}</Typography>}
      {pool.note && <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>{pool.note}</Typography>}
      {!TOO_LITTLE.has(pool.state) && (
        <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }} data-testid="receiving-window">
          {`Figures from calls between ${windowText(windows.check)}; the advice was chosen from calls between `
            + `${windowText(windows.choose)}.`}
        </Typography>
      )}
      {pool.numbers.length > 0 && (
        <TableContainer sx={{ mt: 2 }}>
          <Table size="small" aria-label="Your numbers">
            <TableHead>
              <TableRow>
                <TableCell>Number</TableCell>
                <TableCell align="right">{`Calls, last ${days} days`}</TableCell>
                <TableCell align="right">{`Billed by the minute, last ${days} days (estimate)`}</TableCell>
                <TableCell>Advice</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {pool.numbers.map((row) => (
                <TableRow key={row.number}>
                  <TableCell>{row.number}</TableCell>
                  <TableCell align="right">{row.calls}</TableCell>
                  <TableCell align="right">
                    {row.unpriced_calls ? NOT_PRICED : formatMoneyList(row.billed_by_the_minute, NOT_PRICED)}
                  </TableCell>
                  <TableCell>{adviceText(row)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableContainer>
      )}
      {pooled && pool.check && pool.choose && (
        <TableContainer sx={{ mt: 2 }}>
          <Table size="small" aria-label="Shared lines compared with billing by the minute">
            <TableHead>
              <TableRow>
                <TableCell>Estimate</TableCell>
                <TableCell align="right">{`The ${days} days before`}</TableCell>
                <TableCell align="right">{`The last ${days} days`}</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {COST_ROWS.map(([label, key]) => (
                <TableRow key={key}>
                  <TableCell>{label}</TableCell>
                  <TableCell align="right">{figure(pool.choose?.[key])}</TableCell>
                  <TableCell align="right">{figure(pool.check?.[key])}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableContainer>
      )}
      {pooled && (
        <Typography variant="body2" sx={{ mt: 1 }}>{`Most calls at once in the last ${days} days: ${pool.needed ?? 0}.`}</Typography>
      )}
      {(pool.busy_windows ?? []).map((stretch) => (
        <Typography key={stretch.start} variant="body2" sx={{ mt: 1 }} data-testid="receiving-busy">
          {`All shared lines busy from ${momentText(stretch.start)} to ${momentText(stretch.end)}: `
            + `${stretch.turned_away} ${stretch.turned_away === 1 ? 'caller' : 'callers'} would have heard a busy signal.`}
        </Typography>
      ))}
      {pool.break_even && <Typography variant="body2" sx={{ mt: 1 }}>{pool.break_even}</Typography>}
      {(pool.assumptions ?? []).length > 0 && (
        <Accordion disableGutters elevation={0} sx={{ mt: 1, '&:before': { display: 'none' } }}>
          <AccordionSummary expandIcon={<ExpandMoreIcon />}>
            <Typography variant="body2">What these estimates assume</Typography>
          </AccordionSummary>
          <AccordionDetails>
            <Stack spacing={0.5}>
              {pool.assumptions?.map((line) => <Typography key={line} variant="body2">{line}</Typography>)}
            </Stack>
          </AccordionDetails>
        </Accordion>
      )}
    </Paper>
  );
}

function QuietNumbers({ advice }: { advice: Advice }) {
  const quiet = advice.quiet_numbers;
  // Shown once you add your NPI and Faxbot has read it (Your NPI record, below).
  const npi = quiet.numbers.some((row) => row.npi_record);
  return (
    <Paper variant="outlined" sx={{ p: 2, borderRadius: 2 }} data-testid="receiving-quiet">
      <Heading title="Numbers with few calls" />
      <Typography variant="body1">{quiet.sentence}</Typography>
      {quiet.numbers.length > 0 && (
        <TableContainer sx={{ mt: 2 }}>
          <Table size="small" aria-label="Numbers with few calls">
            <TableHead>
              <TableRow>
                <TableCell>Number</TableCell>
                <TableCell align="right">{`Received, last ${advice.days} days`}</TableCell>
                <TableCell align="right">{`Sent, last ${advice.days} days`}</TableCell>
                <TableCell align="right">Rental a month (estimate)</TableCell>
                {npi && <TableCell>On your NPI record</TableCell>}
              </TableRow>
            </TableHead>
            <TableBody>
              {quiet.numbers.map((row) => (
                <TableRow key={row.number}>
                  <TableCell>{row.number}</TableCell>
                  <TableCell align="right">{row.received}</TableCell>
                  <TableCell align="right">{row.sent}</TableCell>
                  <TableCell align="right">{formatMoneyList(row.monthly_rental, 'No price yet')}</TableCell>
                  {npi && <TableCell>{row.npi_record?.state === 'listed' ? 'Yes: keep it' : 'No'}</TableCell>}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableContainer>
      )}
      {quiet.numbers.length > 0 && (
        <Typography variant="body2" sx={{ mt: 1.5 }} data-testid="receiving-quiet-question">{STILL_PUBLISHED}</Typography>
      )}
    </Paper>
  );
}

export const STILL_PUBLISHED = 'Before you give up a number, ask: is it still printed on your letterhead, forms or '
  + 'website, or listed anywhere? If it is, keep it.';

// HumbleFax's and eFax's own numbers: their faxes in the window and the plan they come with (advice only).
function ServiceNumbers({ advice }: { advice: Advice }) {
  const services = advice.provider_numbers;
  if (!services || services.state === 'none') return null;
  return (
    <Paper variant="outlined" sx={{ p: 2, borderRadius: 2 }} data-testid="receiving-service-numbers">
      <Heading title="Fax service numbers" />
      <Typography variant="body1">{services.sentence}</Typography>
      <TableContainer sx={{ mt: 2 }}>
        <Table size="small" aria-label="Fax service numbers">
          <TableHead>
            <TableRow>
              <TableCell>Fax service</TableCell>
              <TableCell>Number</TableCell>
              <TableCell align="right">{`Received, last ${advice.days} days`}</TableCell>
              <TableCell align="right">{`Sent, last ${advice.days} days`}</TableCell>
              <TableCell align="right">Plan a month</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {services.numbers.map((row) => (
              <TableRow key={`${row.provider}-${row.number}`}>
                <TableCell>{row.name}</TableCell>
                <TableCell>{row.number}</TableCell>
                <TableCell align="right">{row.received}</TableCell>
                <TableCell align="right">{row.sent}</TableCell>
                <TableCell align="right">{formatMoneyList(row.plan_fee, 'No price yet')}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableContainer>
      <Stack spacing={0.5} sx={{ mt: 1.5 }}>
        {services.numbers.filter((row) => row.sentence !== services.sentence || row.question).map((row) => (
          <Typography key={`${row.provider}-${row.number}-sentence`} variant="body2">
            {row.question ? `${row.sentence} ${row.question}` : row.sentence}
          </Typography>
        ))}
      </Stack>
    </Paper>
  );
}

function FaxServices({ advice }: { advice: Advice }) {
  const { connections } = advice;
  return (
    <Paper variant="outlined" sx={{ p: 2, borderRadius: 2 }} data-testid="receiving-services">
      <Heading title="Fax services" />
      <Typography variant="body1">{connections.sentence}</Typography>
      {connections.items.length > 1 && (
        <TableContainer sx={{ mt: 2 }}>
          <Table size="small" aria-label="Fax services">
            <TableHead>
              <TableRow>
                <TableCell>Fax service</TableCell>
                <TableCell align="right">Monthly fee (estimate)</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {connections.items.map((item) => (
                <TableRow key={item.name}>
                  <TableCell>{item.name}</TableCell>
                  <TableCell align="right">{formatMoneyList(item.monthly_fee, 'No price yet')}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableContainer>
      )}
    </Paper>
  );
}

function Prices({ advice }: { advice: Advice }) {
  if (advice.prices.length === 0) return null;
  return (
    <Accordion disableGutters variant="outlined" sx={{ borderRadius: 2 }}>
      <AccordionSummary expandIcon={<ExpandMoreIcon />}>
        <Typography variant="body2">Published prices used</Typography>
      </AccordionSummary>
      <AccordionDetails>
        <Stack spacing={0.5}>
          {advice.prices.map((price) => (
            <Typography key={price.label} variant="body2">
              {`${price.label}: ${price.text}. Read ${readOnText(price.read_on)} from `}
              {price.source_url
                ? <Link href={price.source_url} target="_blank" rel="noopener noreferrer">{new URL(price.source_url).host}</Link>
                : 'the provider'}
              .
            </Typography>
          ))}
        </Stack>
      </AccordionDetails>
    </Accordion>
  );
}

// Too little history (or no carrier line) is one sentence; the quiet numbers wait for the same history.
function waitingForHistory(advice: Advice): boolean {
  return TOO_LITTLE.has(advice.pool.state)
    && advice.quiet_numbers.state !== 'quiet' && advice.quiet_numbers.state !== 'none_quiet';
}

export default function ReceivingRecommendations({ client, onCount }: {
  client: AdminAPIClient;
  // Recommendations' empty sentence: 0 while Faxbot waits for call history, 1 once it advises; null on failure.
  onCount?: (count: number | null) => void;
}) {
  const [advice, setAdvice] = useState<Advice | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const loaded = await client.getReceivingRecommendations();
      setAdvice(loaded);
      onCount?.(waitingForHistory(loaded) && loaded.provider_numbers?.state !== 'quiet' ? 0 : 1);
    } catch (failure) {
      setError(failure);
      onCount?.(null);
    } finally {
      setBusy(false);
    }
  }, [client, onCount]);

  useEffect(() => { void load(); }, [load]);

  const waiting = advice !== null && waitingForHistory(advice);
  return (
    <Box data-testid="receiving-recommendations">
      <Typography variant="h5" component="h2" sx={{ mb: 1 }}>Receiving</Typography>
      <DeliveryError error={error} onClose={() => setError(null)} />
      {!advice && busy && <CircularProgress size={24} />}
      {advice && (
        <Stack spacing={2}>
          {waiting ? (
            <Paper variant="outlined" sx={{ p: 2, borderRadius: 2 }} data-testid="receiving-lines">
              <Typography variant="body1" data-testid="receiving-sentence">{advice.pool.sentence}</Typography>
            </Paper>
          ) : (
            <>
              <SharedLines advice={advice} />
              <QuietNumbers advice={advice} />
            </>
          )}
          <ServiceNumbers advice={advice} />
          <FaxServices advice={advice} />
          <Prices advice={advice} />
        </Stack>
      )}
    </Box>
  );
}
