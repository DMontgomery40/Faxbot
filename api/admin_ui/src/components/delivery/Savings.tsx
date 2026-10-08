// Costs → Savings: what each way Faxbot saves money saved, and the only place the amounts appear.
// Every money figure is an estimate: it compares what Faxbot sent with calls and pages that never
// happened, so it stays an estimate after the carrier reports. Each part has an anchor
// (#/costs/savings?part=sslfax): the Overview's savings map links to it, and the part opened that way
// is scrolled to and outlined.
import { useCallback, useEffect, useState } from 'react';
import { Box, Chip, CircularProgress, Paper, Stack, Typography } from '@mui/material';
import AdminAPIClient from '../../api/client';
import type { Savings as SavingsResult } from '../../api/deliveryTypes';
import { parseServerTime } from '../../api/time';
import { ScreenHeader } from '../access/AccessViews';
import { DeliveryError, formatMoneyList } from './shared';

// The day case packets started being counted, in the reader's own words.
export function countedFromSentence(packets: SavingsResult['case_packets']): string | null {
  if (!packets.earlier_not_counted) return null;
  if (!packets.counted_from) return packets.counted_from_sentence;
  const day = parseServerTime(packets.counted_from);
  if (!day) return packets.counted_from_sentence;
  const date = day.toLocaleDateString(undefined, { day: 'numeric', month: 'long', year: 'numeric' });
  return `Counted from ${date}, when Faxbot started recording what each packet left out.`;
}

// The element id of one part's anchor.
export function savingsPartId(part: string): string {
  return `savings-part-${part}`;
}

function Part({ part, title, sentence, note, testId, focus, estimate = true }: {
  part: string; title: string; sentence: string; note?: string | null; testId: string; focus?: string | null;
  estimate?: boolean;
}) {
  const focused = focus === part;
  return (
    <Paper variant="outlined" id={savingsPartId(part)} data-testid={testId} data-focused={focused ? 'true' : undefined}
      sx={{ p: 2, borderRadius: 2, scrollMarginTop: 80,
        ...(focused ? { borderColor: 'primary.main', borderWidth: 2, boxShadow: 3 } : {}) }}>
      <Box display="flex" alignItems="center" gap={1} sx={{ mb: 1 }}>
        <Typography variant="h6" component="h2">{title}</Typography>
        {estimate && <Chip size="small" variant="outlined" label="Estimate" />}
      </Box>
      <Typography variant="body2">{sentence}</Typography>
      {note && <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>{note}</Typography>}
    </Paper>
  );
}

export default function Savings({ client, focus = null }: { client: AdminAPIClient; focus?: string | null }) {
  const [data, setData] = useState<SavingsResult | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      setData(await client.getSavings());
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  }, [client]);

  useEffect(() => { void load(); }, [load]);

  // The part the address names comes into view once the parts are on the page.
  useEffect(() => {
    if (!data || !focus) return;
    document.getElementById(savingsPartId(focus))?.scrollIntoView?.({ behavior: 'smooth', block: 'start' });
  }, [data, focus]);

  return (
    <Box>
      <ScreenHeader title="Savings" subtitle={data?.sentence} onRefresh={() => void load()} busy={busy} />
      <DeliveryError error={error} onClose={() => setError(null)} />
      {!data && busy && <CircularProgress size={24} />}
      {data && (
        <Stack spacing={2}>
          <Typography variant="body1" data-testid="savings-total">
            {/* The server's headline says honestly when something cost more than it saved. */}
            {data.total_sentence ?? (data.total_saved.length
              ? `About ${formatMoneyList(data.total_saved)} saved in the last ${data.days} days.`
              : `No money saved in the last ${data.days} days, as far as Faxbot can tell.`)}
          </Typography>
          <Part part="sending_together" title="Sending together" sentence={data.sending_together.sentence}
            testId="savings-together" focus={focus} />
          {data.separator_pages && (
            <Part part="separator_pages" title="Separator pages left out" sentence={data.separator_pages.sentence}
              testId="savings-separator-pages" focus={focus} />
          )}
          <Part part="direct_delivery" title="Direct delivery" sentence={data.direct_delivery.sentence}
            testId="savings-direct" focus={focus} />
          {data.direct_fax_images && (
            <Part part="direct_fax_images" title="Direct fax images" sentence={data.direct_fax_images.sentence}
              testId="savings-fax-images" focus={focus} />
          )}
          <Part part="case_packets" title="Case packets" sentence={data.case_packets.sentence}
            note={countedFromSentence(data.case_packets)} testId="savings-packets" focus={focus} />
          {data.sslfax && <Part part="sslfax" title="Faster pages" sentence={data.sslfax.sentence} testId="savings-sslfax"
            focus={focus} />}
          {data.own_numbers && <Part part="own_numbers" title="Faxes to your own numbers" sentence={data.own_numbers.sentence}
            testId="savings-own" focus={focus} />}
          {data.toll_free && <Part part="toll_free" title="Approved toll-free numbers" sentence={data.toll_free.sentence}
            testId="savings-toll-free" focus={focus} />}
          {data.packing && <Part part="packing" title="Pages saved by packing" sentence={data.packing.sentence}
            testId="savings-packing" focus={focus} />}
          {/* Bytes, counted exactly, and never part of the money total above. */}
          {data.direct_bytes && (
            <Part part="direct_bytes" title="Bytes saved by reuse and patches" sentence={data.direct_bytes.sentence}
              estimate={false} testId="savings-bytes" focus={focus} />
          )}
          {data.encoding && (
            <Part part="encoding" title="Pages saved by encoding (experimental)" sentence={data.encoding.sentence}
              testId="savings-encoding" focus={focus} />
          )}
          {/* Time on the line Faxbot estimated when it lightened the pages; no money is added for it. */}
          {data.fax_friendly && (
            <Part part="fax_friendly" title="Shaded pages lightened" sentence={data.fax_friendly.sentence}
              testId="savings-fax-friendly" focus={focus} />
          )}
          {/* Exact counts with no money: the route each fax took, and why. */}
          {data.cheapest_route && (
            <Part part="cheapest_route" title="Cheapest route per delivered fax" sentence={data.cheapest_route.sentence}
              estimate={false} testId="savings-cheapest-route" focus={focus} />
          )}
          {data.plan_first && (
            <Part part="plan_first" title="Faxes through your plan" sentence={data.plan_first.sentence}
              estimate={false} testId="savings-plan-first" focus={focus} />
          )}
          {data.relay && (
            <Part part="relay" title="Partner relays" sentence={data.relay.sentence} testId="savings-relay"
              focus={focus} />
          )}
          {data.continuation && (
            <Part part="continuation" title="Only the missing pages" sentence={data.continuation.sentence}
              estimate={false} testId="savings-continuation" focus={focus} />
          )}
          {data.partner_repair && (
            <Part part="partner_repair" title="Missing pages to partners" sentence={data.partner_repair.sentence}
              estimate={false} testId="savings-partner-repair" focus={focus} />
          )}
          {/* Measured on the trunk's own calls: seconds a page each way, compared only with enough calls. */}
          {data.t38 && (
            <Part part="t38" title="Fax over IP (T.38)" sentence={data.t38.sentence} estimate={false}
              testId="savings-t38" focus={focus} />
          )}
          {data.digital && (
            <Part part="digital" title="Direct messages and FHIR" sentence={data.digital.sentence} estimate={false}
              testId="savings-digital" focus={focus} />
          )}
          {data.blocked_calls && (
            <Part part="blocked_calls" title="Junk callers turned away" sentence={data.blocked_calls.sentence}
              estimate={false} testId="savings-blocked-calls" focus={focus} />
          )}
        </Stack>
      )}
    </Box>
  );
}
