// Costs → Recommendations: advice from history that never changes a setting or sends anything.
// Fax marker (calls marked as fax against calls not marked), billing steps (calls that end just past a billed
// minute), partner candidates (numbers whose faxes cost the most again and again) and toll-free numbers (used only
// after the recipient's approval is recorded). Each section always says where it stands, including "too few calls".
import { useCallback, useEffect, useState, type ReactNode } from 'react';
import {
  Box, Button, Chip, CircularProgress, Paper, Stack, Table, TableBody, TableCell, TableContainer, TableHead, TableRow,
  Typography,
} from '@mui/material';
import AdminAPIClient from '../../api/client';
import type {
  BillingSteps, FaxMarkerAdvice, FaxMarkerSide, PartnerCandidates, TollFreeRecommendations,
} from '../../api/deliveryTypes';
import type { AdminDestination } from '../../navigation';
import { DeliveryError, formatMoney } from './shared';
import { readOnText } from './ReceivingRecommendations';

type Navigate = (destination: AdminDestination) => void;

// Loads one section's advice and reports how many things it suggests (0 while it has nothing yet; null on failure).
function useAdvice<T>(load: () => Promise<T>, count: (data: T) => number, onCount?: (count: number | null) => void) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const run = useCallback(async () => {
    setError(null);
    try {
      const loaded = await load();
      setData(loaded);
      onCount?.(count(loaded));
    } catch (failure) {
      setError(failure);
      onCount?.(null);
    }
  }, [load, count, onCount]);
  useEffect(() => { void run(); }, [run]);
  return { data, error, clear: () => setError(null) };
}

function Section({ title, testId, estimate = true, error, onClear, loading, children }: {
  title: string; testId: string; estimate?: boolean; error: unknown; onClear: () => void; loading: boolean;
  children: ReactNode;
}) {
  return (
    <Box data-testid={testId}>
      <Box display="flex" alignItems="center" gap={1} sx={{ mb: 1 }}>
        <Typography variant="h5" component="h2">{title}</Typography>
        {estimate && <Chip size="small" variant="outlined" label="Estimate" />}
      </Box>
      <DeliveryError error={error} onClose={onClear} />
      {loading && <CircularProgress size={24} />}
      {!loading && <Paper variant="outlined" sx={{ p: 2, borderRadius: 2 }}>{children}</Paper>}
    </Box>
  );
}

// -- fax marker --------------------------------------------------------------------------------------------

function percent(value: number | null): string {
  return value === null ? '-' : `${value}%`;
}

const MARKER_ROWS: Array<[string, (side: FaxMarkerSide) => string]> = [
  ['Calls', (side) => String(side.calls)],
  ['Delivered', (side) => percent(side.delivered_percent)],
  ['Used fax over IP (T.38)', (side) => percent(side.t38_percent)],
  ['Seconds a page', (side) => (side.seconds_per_page === null ? '-' : String(side.seconds_per_page))],
  ['Cost per delivered fax', (side) => side.cost_text],
];

export function FaxMarkerSection({ client, onCount }: { client: AdminAPIClient; onCount?: (count: number | null) => void }) {
  const load = useCallback(() => client.getFaxMarkerAdvice(), [client]);
  const count = useCallback((data: FaxMarkerAdvice) => (data.state === 'compared' ? 1 : 0), []);
  const { data, error, clear } = useAdvice(load, count, onCount);
  return (
    <Section title="Fax marker" testId="advice-fax-marker" error={error} onClear={clear} loading={!data && !error}>
      {data && (
        <Stack spacing={1}>
          <Typography variant="body1" data-testid="fax-marker-sentence">{data.sentence}</Typography>
          {data.state === 'compared' && (
            <TableContainer>
              <Table size="small" aria-label="Calls marked as fax and calls not marked">
                <TableHead>
                  <TableRow>
                    <TableCell />
                    <TableCell align="right">Marked as fax</TableCell>
                    <TableCell align="right">Not marked</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {MARKER_ROWS.map(([label, value]) => (
                    <TableRow key={label}>
                      <TableCell>{label}</TableCell>
                      <TableCell align="right">{value(data.marked)}</TableCell>
                      <TableCell align="right">{value(data.not_marked)}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableContainer>
          )}
          {data.caveat && <Typography variant="body2" color="text.secondary">{data.caveat}</Typography>}
          {data.left_out_sentence && <Typography variant="body2" color="text.secondary">{data.left_out_sentence}</Typography>}
          <Typography variant="body2" color="text.secondary">{data.setting_sentence}</Typography>
        </Stack>
      )}
    </Section>
  );
}

// -- billing steps -------------------------------------------------------------------------------------------

function pastText(row: { seconds_past: { least: number; most: number } }): string {
  const { least, most } = row.seconds_past;
  return least === most ? `${least} s` : `${least}–${most} s`;
}

export function BillingStepsSection({ client, onCount }: { client: AdminAPIClient; onCount?: (count: number | null) => void }) {
  const load = useCallback(() => client.getBillingSteps(), [client]);
  const count = useCallback((data: BillingSteps) => data.numbers.length, []);
  const { data, error, clear } = useAdvice(load, count, onCount);
  return (
    <Section title="Billing steps" testId="advice-billing-steps" error={error} onClear={clear} loading={!data && !error}>
      {data && (
        <Stack spacing={1}>
          <Typography variant="body1" data-testid="billing-steps-sentence">{data.sentence}</Typography>
          {data.numbers.length > 0 && (
            <TableContainer>
              <Table size="small" aria-label="Calls that end just past a billed step">
                <TableHead>
                  <TableRow>
                    <TableCell>Fax number</TableCell>
                    <TableCell align="right">Calls</TableCell>
                    <TableCell align="right">Just past a step</TableCell>
                    <TableCell align="right">Seconds past</TableCell>
                    <TableCell align="right">Would have saved</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {data.numbers.map((row) => (
                    <TableRow key={row.number}>
                      <TableCell>
                        {row.display_name || row.number}
                        {row.display_name && <Typography variant="body2" color="text.secondary">{row.number}</Typography>}
                      </TableCell>
                      <TableCell align="right">{row.calls}</TableCell>
                      <TableCell align="right">{row.calls_near}</TableCell>
                      <TableCell align="right">{pastText(row)}</TableCell>
                      <TableCell align="right">{formatMoney(row.saving)}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableContainer>
          )}
          {data.step && (
            <Typography variant="body2" color="text.secondary">
              {`${data.carrier ?? 'Your carrier'} bills in ${data.step.seconds}-second steps of ${data.step.price_text}, `
                + `from your price list dated ${readOnText(data.step.read_on)}.`}
            </Typography>
          )}
        </Stack>
      )}
    </Section>
  );
}

// -- partner candidates ------------------------------------------------------------------------------------

export function PartnersSection({ client, onCount, onNavigate }: {
  client: AdminAPIClient; onCount?: (count: number | null) => void; onNavigate?: Navigate;
}) {
  const load = useCallback(() => client.getPartnerCandidates(), [client]);
  const count = useCallback((data: PartnerCandidates) => data.items.length, []);
  const { data, error, clear } = useAdvice(load, count, onCount);
  const enroll = (link: string) => {
    if (onNavigate) onNavigate(link as AdminDestination);
    else window.location.hash = `#/${link}`;
  };
  return (
    <Section title="Partner candidates" testId="advice-partners" error={error} onClear={clear} loading={!data && !error}>
      {data && (
        <Stack spacing={1.5}>
          <Typography variant="body1" data-testid="partners-sentence">{data.sentence}</Typography>
          {data.items.map((item) => (
            <Box key={item.number} data-testid="partner-candidate">
              <Typography variant="subtitle1">{item.display_name || item.number}</Typography>
              {item.display_name && <Typography variant="body2" color="text.secondary">{item.number}</Typography>}
              <Typography variant="body2" sx={{ mt: 0.5 }}>{item.sentence}</Typography>
              <Box display="flex" gap={1} flexWrap="wrap" alignItems="center" sx={{ mt: 1 }}>
                <Chip size="small" variant="outlined"
                  label={item.monthly_cost ? `About ${formatMoney(item.monthly_cost)} a month` : item.cost_text} />
                <Button size="small" variant="outlined" sx={{ borderRadius: 2 }} onClick={() => enroll(item.link)}>
                  {item.link_label}
                </Button>
              </Box>
            </Box>
          ))}
        </Stack>
      )}
    </Section>
  );
}

// -- toll-free numbers -------------------------------------------------------------------------------------

export function TollFreeSection({ client, onCount, onNavigate }: {
  client: AdminAPIClient; onCount?: (count: number | null) => void; onNavigate?: Navigate;
}) {
  const load = useCallback(() => client.getTollFreeRecommendations(), [client]);
  const count = useCallback((data: TollFreeRecommendations) => data.items.length, []);
  const { data, error, clear } = useAdvice(load, count, onCount);
  const open = () => {
    if (onNavigate) onNavigate('recipients/list');
    else window.location.hash = '#/recipients/list';
  };
  return (
    <Section title="Toll-free numbers" testId="advice-toll-free" estimate={false} error={error} onClear={clear}
      loading={!data && !error}>
      {data && (
        <Stack spacing={1.5}>
          <Typography variant="body1" data-testid="toll-free-sentence">{data.sentence}</Typography>
          {data.items.map((item) => (
            <Box key={item.number} data-testid="toll-free-item">
              <Box display="flex" gap={1} alignItems="center" flexWrap="wrap">
                <Typography variant="subtitle1">{item.display_name || item.number}</Typography>
                <Chip size="small" color={item.approved ? 'success' : 'default'} variant="outlined"
                  label={item.approved ? 'Approved' : 'Not approved yet'} />
              </Box>
              <Typography variant="body2" sx={{ mt: 0.5 }}>{item.sentence}</Typography>
            </Box>
          ))}
          <Box>
            <Button size="small" variant="outlined" sx={{ borderRadius: 2 }} onClick={open}>
              Open Recipients
            </Button>
          </Box>
        </Stack>
      )}
    </Section>
  );
}
